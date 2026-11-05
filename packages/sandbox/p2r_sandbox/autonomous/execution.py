"""Docker-isolated execution over a local CLI or strict OpenSSH transport.

SSH credentials and model configuration never enter the experiment container.
State survives client closure; users explicitly manage container cleanup.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path

MAX_OUTPUT = 1_000_000


class ExecutionError(Exception):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code

    def as_dict(self) -> dict:
        return {"message": str(self), "code": self.code}


class ExecutionTarget:
    def __init__(self, config: dict, state_dir: Path):
        self.config = json.loads(json.dumps(config))
        self.state_dir = Path(state_dir).resolve()
        self.kind = config.get('kind')
        self.workspace = config.get('workspace')
        if self.kind not in ('local', 'ssh') or not isinstance(self.workspace, str) or not self.workspace.startswith('/'):
            raise ExecutionError('kind must be local/ssh and workspace must be absolute', 'invalid_config')
        if any(c in self.workspace for c in ('\n', '\x00', ',')) or '..' in self.workspace.split('/') or self.workspace == '/':
            raise ExecutionError('workspace must be a dedicated absolute directory', 'invalid_config')
        self.image = config.get('image', 'paper2run-experiment:1')
        if not isinstance(self.image, str) or self.image.startswith('-'):
            raise ExecutionError('invalid image', 'invalid_config')
        network = config.get('network', {})
        if network.get('host_and_metadata_access') is not False or not isinstance(network.get('public_internet'), bool):
            raise ExecutionError('network requires public_internet boolean and host_and_metadata_access=false', 'invalid_config')
        self.public = network['public_internet']
        if self.kind == 'ssh' and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', config.get('ssh_alias', '')):
            raise ExecutionError('ssh_alias must be an existing OpenSSH config alias', 'invalid_config')
        if self.kind == 'local':
            root = Path(self.workspace)
            if root.is_symlink() or root.resolve() != root or self.state_dir.is_relative_to(root):
                raise ExecutionError('workspace cannot be symlinked or contain control state', 'invalid_config')
        # Stable by target + workspace + configuration; restart/reconnect finds the
        # same Docker object, including after a lost create/start response.
        digest = hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()
        self.container = 'p2r-' + digest[:24]
        self._prepared = False
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _host(self, args: list[str], *, timeout: int = 45, input: str | None = None, check: bool = True):
        argv = args
        if self.kind == 'ssh':
            argv = ['ssh']
            # Optional explicit user-owned config (also useful for isolated sshd
            # transport tests). Never create/modify known_hosts automatically.
            if self.config.get('ssh_config'):
                argv += ['-F', str(self.config['ssh_config'])]
            argv += ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                     '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes',
                     '-o', 'ConnectTimeout=10', '--', self.config['ssh_alias'], shlex.join(args)]
        try:
            result = subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ExecutionError('transport timed out; reconcile using the same launch_id', 'transport_timeout') from exc
        except OSError as exc:
            raise ExecutionError(str(exc), 'transport_unavailable') from exc
        if check and result.returncode:
            text = result.stderr[-16000:]
            if 'Host key verification failed' in text or 'REMOTE HOST IDENTIFICATION HAS CHANGED' in text:
                raise ExecutionError('Unknown or changed SSH host key. Verify identity and enroll in your existing known_hosts. ' + text, 'ssh_host_key')
            if 'Permission denied' in text and self.kind == 'ssh':
                raise ExecutionError(text, 'ssh_authentication')
            raise ExecutionError(text or result.stdout[-16000:], 'transport_error')
        return result

    def _docker(self, *args: str, **kwargs):
        return self._host(['docker', *args], **kwargs)

    def _call(self, op: str, args: dict, *, agent: bool = False, timeout: int = 45) -> dict:
        command = ['exec', '-i', '-u', '0', self.container]
        if agent:
            command += ['setpriv', '--reuid=10001', '--regid=10001', '--clear-groups', '--bounding-set=-all', '--inh-caps=-all', '--ambient-caps=-all', '--no-new-privs']
        # File API uses a trusted script but executes with precisely agent UID.
        command += ['python3', '/opt/p2r-controller.py', op, json.dumps(args)]
        result = self._docker(*command, timeout=timeout, check=False)
        try:
            value = json.loads(result.stdout)
        except ValueError as exc:
            raise ExecutionError(result.stderr[-16000:] or 'invalid controller response', 'protocol_error') from exc
        if value.get('error'):
            raise ExecutionError(value['error'], value.get('code', 'execution_error'))
        if result.returncode:
            raise ExecutionError(result.stderr[-16000:], 'execution_error')
        return value

    def probe(self) -> dict:
        self._docker('info', '--format', '{{.ServerVersion}}')
        image = self._docker('image', 'inspect', self.image, check=False)
        report = {'kind': self.kind, 'container': self.container, 'image': self.image,
                  'image_available': image.returncode == 0, 'ready': False,
                  'network_verified': False, 'storage_enforcement': 'observed',
                  'gpu_devices_requested': self.config.get('gpu_devices', [])}
        if image.returncode:
            report['blocker'] = 'Image missing. Run: bash scripts/autonomous/build-image.sh (on the Docker target); never auto-pull arbitrary images.'
            return report
        inspect = self._docker('inspect', self.container, check=False)
        if inspect.returncode == 0:
            obj = json.loads(inspect.stdout)[0]
            report['container_running'] = obj['State']['Running']
            if obj['State']['Running']:
                self._check_address_identity(obj)
                report.update(self._call('resources', {}))
                verified = self._network()
                report.update(network=verified, network_verified=self._network_ok(verified))
                report['ready'] = report['network_verified']
        return report

    def _host_addresses(self):
        # getifaddrs covers aliases and additional public management interfaces,
        # including addresses absent from hostname DNS. Run on the Docker target.
        script = Path(__file__).with_name('_interfaces.py').read_text()
        try:
            values = json.loads(self._host(['python3', '-I', '-c', script]).stdout)
            if not isinstance(values, list) or not values:
                raise ValueError('no IPv4 interfaces')
            return sorted({str(ipaddress.IPv4Address(x)) for x in values})
        except (ValueError, TypeError) as exc:
            raise ExecutionError('Cannot enumerate target IPv4 interfaces', 'interface_enumeration_failed') from exc

    def _current_addresses(self, obj):
        addresses = self._host_addresses()
        inside = self._docker('exec', self.container, 'python3', '-I', '-c',
                              'import socket,json; a=set();\nfor n in ("host.docker.internal","gateway.docker.internal"):\n try: a.update(x[4][0] for x in socket.getaddrinfo(n,None,socket.AF_INET))\n except OSError: pass\nprint(json.dumps(sorted(a)))')
        addresses += json.loads(inside.stdout)
        addresses += [x['Gateway'] for x in obj['NetworkSettings']['Networks'].values() if x.get('Gateway')]
        return sorted({str(ipaddress.IPv4Address(x)) for x in addresses})

    def _policy_reject_networks(self):
        # Address churn is safe only when the unchanged policy still rejects
        # the entire newly observed address. A preceding broad ACCEPT means a
        # later REJECT is not a usable coverage proof.
        saved = json.loads((self.state_dir / (self.container + '.json')).read_text())
        identity = saved.get('identity', {})
        if identity.get('policy') != self._policy_fingerprint():
            return []
        networks = []
        rules = self._docker('exec', '-u', '0', self.container, 'iptables-save').stdout
        broad_accept_before = False
        for line in rules.splitlines():
            if '-A OUTPUT' not in line:
                continue
            if '-j ACCEPT' in line:
                tokens = line.split()
                # Only an unconditional OUTPUT ACCEPT defeats a later
                # destination REJECT. Protocol/owner/source-scoped accepts do
                # not permit arbitrary traffic to the new host address.
                broad_accept_before = tokens[2:] == ['-j', 'ACCEPT']
                continue
            if broad_accept_before or '-j REJECT' not in line:
                continue
            match = re.search(r'\s-d\s+(\S+)', line)
            if not match:
                continue
            try:
                networks.append(ipaddress.ip_network(match.group(1), strict=False))
            except ValueError:
                continue
        return networks

    def _check_address_identity(self, obj):
        saved = set(self._addresses())
        current = set(self._current_addresses(obj))
        added = current - saved
        if added:
            covered = self._policy_reject_networks()
            uncovered = [address for address in sorted(added)
                         if not any(ipaddress.ip_address(address) in network for network in covered)]
            if uncovered:
                self._prepared = False
                raise ExecutionError('Target IPv4 interfaces or Docker gateway changed; new addresses are not covered by the existing isolation policy: '
                                     + ', '.join(uncovered), 'host_addresses_changed')

    def _policy_fingerprint(self):
        texts = [self._docker('exec', '-u', '0', self.container, tool).stdout
                 for tool in ('iptables-save', 'ip6tables-save')]
        normalized = '\n'.join(line for text in texts for line in text.splitlines()
                               if not line.startswith('#'))
        normalized = re.sub(r'\[\d+:\d+\]', '[0:0]', normalized)
        return hashlib.sha256(normalized.encode()).hexdigest()

    def _verify_generation(self, obj):
        saved = json.loads((self.state_dir / (self.container + '.json')).read_text())
        current = {'id': obj['Id'], 'started_at': obj['State']['StartedAt'],
                   'policy': self._policy_fingerprint()}
        if saved.get('identity') != current:
            self._prepared = False
            raise ExecutionError('Container identity, start generation or firewall policy changed; execution blocked', 'isolation_changed')

    def _policy_status(self):
        rules = self._docker('exec', '-u', '0', self.container, 'iptables-save', '-c').stdout
        # A denial errno alone may be an unreachable route. Require actual
        # kernel policy and hit counters for the explicit rejection rules.
        rejects = [line for line in rules.splitlines() if '-A OUTPUT' in line and '-j REJECT' in line]
        policy_verified = ('-P OUTPUT DROP' in rules or ':OUTPUT DROP' in rules) and bool(rejects) and any(not line.startswith('[0:0]') for line in rejects)
        ipv6_rules = self._docker('exec', '-u', '0', self.container, 'ip6tables-save').stdout
        return {'policy_verified': policy_verified,
                'ipv6_policy_verified': ':OUTPUT DROP' in ipv6_rules and '-A OUTPUT' not in ipv6_rules}

    def _network(self):
        result = self._call('network_probe', {'host_addresses': self._addresses()}, agent=True)
        result.update(self._policy_status())
        result['private_denied'] = result['private_denied'] and result['policy_verified'] and result['ipv6_policy_verified']
        return result

    def _revalidate_network_policy(self):
        # On an existing container, identity and firewall fingerprints plus
        # address coverage are checked on every action. Active reachability
        # probes are intentionally not repeated: public=false would otherwise
        # spend up to 12 seconds per tool call waiting for example.com.
        record = json.loads((self.state_dir / (self.container + '.json')).read_text())
        result = dict(record.get('network', {}))
        result.update(self._policy_status())
        result['private_denied'] = result.get('private_denied', False) and result['policy_verified'] and result['ipv6_policy_verified']
        return result

    def _addresses(self):
        path = self.state_dir / (self.container + '.json')
        if not path.exists():
            raise ExecutionError('Missing network identity record; prepare before using this target', 'not_prepared')
        return json.loads(path.read_text())['host_addresses']

    def _network_ok(self, result):
        return (result['private_denied'] and result['uid'] == 10001 and result['caps_zero']
                and result['no_new_privileges'] and result['public'] == self.public)

    def prepare(self) -> dict:
        # A failed refresh must invalidate any previous authorization to execute.
        self._prepared = False
        self._docker('info', '--format', '{{.ServerVersion}}')
        if self._docker('image', 'inspect', self.image, check=False).returncode:
            raise ExecutionError('Missing image ' + self.image + '; run bash scripts/autonomous/build-image.sh on target', 'image_missing')
        inspect = self._docker('inspect', self.container, check=False)
        if inspect.returncode == 0:
            obj = json.loads(inspect.stdout)[0]
            if not obj['State']['Running']:
                raise ExecutionError('Existing container stopped/restarted; inspect interrupted jobs before explicitly replacing it', 'container_stopped')
            self._verify_generation(obj)
            self._check_address_identity(obj)
            network = self._revalidate_network_policy()
            if not self._network_ok(network):
                raise ExecutionError('Existing container failed isolation policy validation', 'network_isolation_failed')
            self._prepared = True
            return {'ready': True, 'container': self.container, 'network': network, 'storage_enforcement': 'observed'}
        # Host operations are fixed setup only; no experiment shell runs here.
        script = 'import os,sys; p=sys.argv[1]; os.makedirs(p,mode=0o700,exist_ok=True); assert os.path.realpath(p)==p; assert os.path.isdir(p)'
        self._host(['python3', '-c', script, self.workspace])
        network_name = self.container + '-net'
        if self._docker('network', 'inspect', network_name, check=False).returncode:
            self._docker('network', 'create', network_name)
        args = ['create', '--name', self.container, '--label', 'paper2run.autonomous=1',
                '--network', network_name, '--cap-drop', 'ALL', '--cap-add', 'NET_ADMIN',
                '--cap-add', 'SETUID', '--cap-add', 'SETGID', '--cap-add', 'CHOWN',
                '--cap-add', 'DAC_OVERRIDE', '--cap-add', 'KILL', '--cap-add', 'SETPCAP',
                '--security-opt', 'no-new-privileges', '--pids-limit', '1024',
                '--mount', f'type=bind,src={self.workspace},dst=/work',
                '--workdir', '/work']
        devices = self.config.get('gpu_devices', [])
        if devices:
            if not all(re.fullmatch(r'[A-Za-z0-9_.:-]+', str(d)) for d in devices):
                raise ExecutionError('invalid GPU device identifier', 'invalid_config')
            args += ['--gpus', '"device=' + ','.join(devices) + '"']
        args += [self.image, 'sleep', 'infinity']
        self._docker(*args)
        helper = Path(__file__).with_name('_container.py').read_text()
        self._docker('start', self.container)
        # Inject trusted controller via stdin, never mount source/state/credentials.
        self._docker('exec', '-i', '-u', '0', self.container, 'python3', '-c',
                     "import sys; f=open('/opt/p2r-controller.py','w'); f.write(sys.stdin.read()); f.close()", input=helper)
        obj = json.loads(self._docker('inspect', self.container).stdout)[0]
        host_ips = self._current_addresses(obj)
        record = self.state_dir / (self.container + '.json')
        record.write_text(json.dumps({'host_addresses': host_ips}))
        try:
            self._call('setup', {'host_addresses': host_ips, 'public_internet': self.public}, timeout=90)
            network = self._network()
            record.write_text(json.dumps({'host_addresses': host_ips, 'network': network, 'identity': {
                'id': obj['Id'], 'started_at': obj['State']['StartedAt'], 'policy': self._policy_fingerprint()}}))
            if not self._network_ok(network):
                raise ExecutionError('Network isolation probe failed: ' + json.dumps(network), 'network_isolation_failed')
        except Exception:
            # Failed setup never exposes a usable experiment container.
            self._docker('stop', '-t', '1', self.container, check=False)
            raise
        self._prepared = True
        return {'ready': True, 'container': self.container, 'workspace': self.workspace,
                'network': network, 'storage_enforcement': 'observed',
                'storage_bytes_requested': self.config.get('storage_bytes'), **self._call('resources', {})}

    def _ready(self):
        # Docker restarts reset network rules without changing this Python object.
        # Re-enumerate host addresses and validate boot identity and policy before
        # every action that can launch or touch experiment workspace data.
        self.prepare()

    @staticmethod
    def _timeout(value):
        if type(value) is not int or not 1 <= value <= 31_536_000:
            raise ExecutionError('timeout_seconds must be a positive integer <= one year', 'invalid_argument')
        return value

    @staticmethod
    def _range(offset, limit):
        if type(offset) is not int or type(limit) is not int or offset < 0 or limit < 0:
            raise ExecutionError('offset/limit must be nonnegative integers', 'invalid_argument')
        return {'offset': offset, 'limit': min(limit, MAX_OUTPUT)}

    def start(self, command: str, launch_id: str, timeout_seconds: int) -> dict:
        if not isinstance(launch_id, str) or not 1 <= len(launch_id) <= 256:
            raise ExecutionError('launch_id must have 1..256 characters', 'invalid_argument')
        if not isinstance(command, str) or len(command.encode()) > MAX_OUTPUT:
            raise ExecutionError('command must be a string <= 1MB', 'invalid_argument')
        seconds = self._timeout(timeout_seconds)
        self._ready()
        job = hashlib.sha256(launch_id.encode()).hexdigest()
        return self._call('start', {'command': command, 'job_id': job, 'timeout_seconds': seconds})

    def execute(self, command: str, timeout_seconds: int = 60) -> dict:
        job = self.start(command, uuid.uuid4().hex, timeout_seconds)['job_id']
        while True:
            result = self._call('result', {'job_id': job})
            if result['state'] == 'dispatching':
                raise ExecutionError('Launch reserved but dispatch acknowledgement is unknown; reconcile job_id, do not relaunch', 'launch_unknown')
            if result['state'] not in ('pending', 'starting', 'running'):
                return result
            time.sleep(.1)

    def status(self, job_id: str) -> dict:
        # Observation must work after reboot even when isolation must be
        # re-established before launching new experiment code.
        return self._call('status', {'job_id': job_id})

    def logs(self, job_id: str, offset: int = 0, limit: int = 16000) -> dict:
        bounds = self._range(offset, limit)
        return self._call('logs', {'job_id': job_id, **bounds})

    def stop(self, job_id: str) -> dict:
        return self._call('stop', {'job_id': job_id})

    def read(self, path: str, offset: int = 0, limit: int = 16000) -> dict:
        bounds = self._range(offset, limit)
        self._ready()
        return self._call('read', {'path': path, **bounds}, agent=True)

    def write(self, path: str, content: str) -> dict:
        if not isinstance(content, str) or len(content.encode()) > MAX_OUTPUT:
            raise ExecutionError('content must be UTF-8 text <= 1MB', 'invalid_argument')
        self._ready()
        return self._call('write', {'path': path, 'content': content}, agent=True)

    def collect(self, path: str, destination: Path) -> dict:
        self._ready()
        result = self._call('collect', {'path': path}, agent=True, timeout=120)
        try:
            data = base64.b64decode(result.pop('data'), validate=True)
            if hashlib.sha256(data).hexdigest() != result['sha256']:
                raise ValueError('checksum mismatch')
            dest = Path(destination)
            dest.parent.mkdir(parents=True, exist_ok=True)
            temporary = dest.with_name(dest.name + '.' + uuid.uuid4().hex + '.tmp')
            with temporary.open('xb') as stream:
                stream.write(data)
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != result['sha256']:
                raise ValueError('local checksum mismatch')
            os.replace(temporary, dest)
        except (ValueError, OSError) as exc:
            raise ExecutionError(str(exc), 'collection_failed') from exc
        return {**result, 'destination': str(dest)}

    def close(self) -> None:
        # No persistent SSH connection exists. Crucially, no stop/rm action here.
        # A retained running container is untouched; future calls revalidate it.
        self._prepared = False
