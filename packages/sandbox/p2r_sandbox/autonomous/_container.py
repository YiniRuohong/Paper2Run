"""Trusted container-side controller. Copied into a read-only root-owned path.

No p2r imports: this file also runs in the experiment image's Python 3.12.
The controller state is root-only and never exposed to experiment processes.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import selectors
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

CONTROL = Path('/var/lib/p2r-control')
LIMIT = 1_000_000
FILE_LIMIT = 64 * 1024 * 1024


def emit(value):
    print(json.dumps(value), flush=True)


def save(path, data):
    temp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    with temp.open('w') as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def boot():
    # boot_id alone does not change on a container restart; PID 1 start time does.
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip() + ':' + identity(1)


def identity(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return ''


def job_path(job):
    if not isinstance(job, str) or not re.fullmatch(r'[a-f0-9]{64}', job):
        raise ValueError('invalid job_id')
    return CONTROL / job


def uid_members(uid):
    result = []
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            status = Path(f'/proc/{name}/status').read_text()
            real_uid = int(next(line.split()[1] for line in status.splitlines() if line.startswith('Uid:')))
            proc_state = next(line.split()[1] for line in status.splitlines() if line.startswith('State:'))
            if real_uid == uid and proc_state != 'Z':
                result.append(int(name))
        except (FileNotFoundError, ProcessLookupError, StopIteration):
            pass
    return result


def kill_uid(uid):
    # UID is allocated monotonically per job and is never shared/reused.
    members = uid_members(uid)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in members:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        until = time.monotonic() + (0.25 if sig == signal.SIGTERM else 2)
        while time.monotonic() < until:
            members = uid_members(uid)
            if not members:
                return True
            time.sleep(.025)
    return not uid_members(uid)


def group_members(pgid):
    result = []
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            parts = Path(f'/proc/{name}/stat').read_text().rsplit(')', 1)[1].split()
            if int(parts[2]) == pgid and parts[0] != 'Z':
                result.append(int(name))
        except (FileNotFoundError, ProcessLookupError):
            pass
    return result


def descendants(parent):
    table = {}
    for name in os.listdir('/proc'):
        if name.isdigit():
            try:
                parts = Path(f'/proc/{name}/stat').read_text().rsplit(')', 1)[1].split()
                if parts[0] != 'Z':
                    table[int(name)] = int(parts[1])
            except (FileNotFoundError, ProcessLookupError):
                pass
    found = {parent}
    while True:
        more = {pid for pid, ppid in table.items() if ppid in found}
        if more <= found:
            return found - {parent}
        found |= more


def terminate(pgid, tree=False):
    # The worker is a subreaper, so double-fork/setsid children also remain its
    # descendants. Never select processes belonging to another job supervisor.
    def members():
        return set(group_members(pgid)) | (descendants(os.getpid()) if tree else set())
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in members():
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        until = time.monotonic() + (1 if sig == signal.SIGTERM else 2)
        while time.monotonic() < until:
            if not members():
                return True
            time.sleep(.05)
    return not members()


def finish(state_path, terminal, **updates):
    # Worker and guardian can finish concurrently after killing the same UID.
    # Serialize read/merge/write so a stale default 'exited' cannot erase a
    # confirmed stop/timeout, and cleanup failure always remains visible.
    with (state_path.parent / 'terminal.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(state_path.read_text())
        previous = state['state']
        if 'stop_failed' in (previous, terminal):
            terminal = 'stop_failed'
        elif previous in ('stopped', 'timeout', 'interrupted'):
            terminal = previous
        elif terminal == 'exited' and (state_path.parent / 'cancel').exists():
            terminal = 'stopped'
        state.update(updates, state=terminal)
        save(state_path, state)


def guardian(job, uid):
    directory = job_path(job)
    path = directory / 'state.json'
    while True:
        try:
            state = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            return
        if state.get('state') not in ('starting', 'running'):
            return
        if state.get('boot_id') != boot():
            return
        expired = time.time() >= state.get('deadline_epoch', 0)
        supervisor_alive = identity(state.get('supervisor_pid', -1)) == state.get('supervisor_start')
        if (directory / 'cancel').exists() or expired or not supervisor_alive:
            reason = 'stopped' if (directory / 'cancel').exists() else ('timeout' if expired else 'interrupted')
            clean = kill_uid(uid)
            finish(path, reason if clean else 'stop_failed',
                   reason='supervisor disappeared' if not supervisor_alive else None)
            return
        time.sleep(.25)


def run_worker(job, uid, timeout_seconds):
    import ctypes
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError('cannot enable child subreaper')
    directory = job_path(job)
    spec = json.loads((directory / 'spec.json').read_text())
    state_path = directory / 'state.json'
    if uid < 20000 or uid > 60000:
        raise ValueError('invalid per-job uid')
    state = {'job_id': job, 'state': 'starting', 'exit_code': None, 'job_uid': uid,
             'boot_id': boot(), 'supervisor_pid': os.getpid(),
             'supervisor_start': identity(os.getpid()), 'offset': 0,
             'deadline_epoch': time.time() + timeout_seconds}
    save(state_path, state)
    subprocess.Popen([sys.executable, __file__, 'guardian', json.dumps({'job_id': job, 'uid': uid})],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    # The shell and every descendant share this unique
    # UID. Neither has capabilities; control state remains root-only.
    args = ['setpriv', f'--reuid={uid}', '--regid=10001', '--clear-groups',
            '--bounding-set=-all', '--inh-caps=-all', '--ambient-caps=-all',
            '--no-new-privs', '/bin/bash', '-c', spec['command']]
    env = {'HOME': '/home/agent', 'USER': 'agent', 'LANG': 'C.UTF-8',
           'PATH': '/work/.venv/bin:/usr/local/bin:/usr/bin:/bin',
           'VIRTUAL_ENV': '/work/.venv', 'PIP_REQUIRE_VIRTUALENV': 'true'}
    env.update(USER=f'job-{uid}', LOGNAME=f'job-{uid}',
               HOME=f'/tmp/p2r-job-{uid}', P2R_JOB_UID=str(uid))
    Path(env['HOME']).mkdir(mode=0o700, exist_ok=True)
    os.chown(env['HOME'], uid, 10001)
    os.umask(0o002)
    child = subprocess.Popen(args, cwd='/work', env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True)
    state.update(state='running', pid=child.pid, process_start=identity(child.pid))
    save(state_path, state)
    selector = selectors.DefaultSelector()
    for stream, name in ((child.stdout, 'stdout'), (child.stderr, 'stderr')):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    streams = {name: (directory / name).open('wb', buffering=0) for name in ('stdout', 'stderr')}
    log = (directory / 'log').open('wb', buffering=0)
    counts = {'stdout': 0, 'stderr': 0, 'log': 0, 'total': 0}
    deadline = time.monotonic() + spec['timeout_seconds']
    terminal = 'exited'
    killed = False
    try:
        while selector.get_map() or child.poll() is None:
            if not killed and ((directory / 'cancel').exists() or time.monotonic() >= deadline):
                terminal = 'stopped' if (directory / 'cancel').exists() else 'timeout'
                kill_uid(uid)
                killed = True
            for key, _ in selector.select(.05):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                name = key.data
                n = max(0, LIMIT - counts['total'])
                kept = chunk[:n]
                streams[name].write(kept)
                log.write(kept)
                counts[name] += len(chunk)
                counts['log'] += len(kept)
                counts['total'] += len(kept)
            # Even if shell exited, children retaining pipe handles remain subject
            # to the deadline. Normal completion also cleans background children.
            if child.poll() is not None and not selector.get_map():
                break
        exit_code = child.wait()
        clean = kill_uid(uid)
        finish(state_path, terminal if clean else 'stop_failed', exit_code=exit_code,
               offset=min(counts['log'], LIMIT), output_truncated=any(v > LIMIT for v in counts.values()))
    finally:
        selector.close()
        for f in streams.values():
            f.close()
        log.close()


def status(job):
    directory = job_path(job)
    path = directory / 'state.json'
    if not directory.exists():
        raise ValueError('job_not_found')
    if not path.exists():
        return {'job_id': job, 'state': 'pending', 'exit_code': None, 'offset': 0}
    data = json.loads(path.read_text())
    if data['state'] in ('running', 'starting'):
        if data['boot_id'] != boot():
            data.update(state='interrupted', reason='container restarted', exit_code=None)
        elif identity(data['supervisor_pid']) != data['supervisor_start']:
            data.update(state='interrupted', reason='supervisor disappeared', exit_code=None)
            # UID allocation is root-only and never reused, even after setsid,
            # reparenting or shell exit. Do not rely on a dead ancestor PID.
            clean = kill_uid(data['job_uid']) if 'job_uid' in data else False
            data['state'] = 'interrupted' if clean else 'stop_failed'
            save(path, data)
        else:
            data['offset'] = min((directory / 'log').stat().st_size, LIMIT) if (directory / 'log').exists() else 0
    return data


def safe_open(path, flags, create_parents=False):
    # Reject *all* symlink components; dirfd/O_NOFOLLOW avoids check/use races.
    if not isinstance(path, str) or path.startswith('/') or '\x00' in path:
        raise ValueError('path_escape')
    parts = path.split('/')
    if any(x in ('', '.', '..') for x in parts):
        raise ValueError('path_escape')
    fd = os.open('/work', os.O_DIRECTORY | os.O_RDONLY)
    try:
        for part in parts[:-1]:
            if create_parents:
                try:
                    os.mkdir(part, mode=0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            new = os.open(part, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
        return os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644, dir_fd=fd)
    finally:
        os.close(fd)


def file_op(op, args):
    import stat
    flags = os.O_RDONLY if op in ('read', 'collect') else os.O_WRONLY | os.O_CREAT
    fd = safe_open(args['path'], flags, op == 'write')
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('path_escape: regular non-hardlinked file required')
        if op == 'write':
            data = args['content'].encode()
            if len(data) > LIMIT:
                raise ValueError('content_too_large')
            os.ftruncate(fd, 0)
            with os.fdopen(fd, 'wb', closefd=False) as f:
                f.write(data)
                f.flush()
            return {'path': args['path'], 'bytes': len(data)}
        if op == 'collect':
            if info.st_size > FILE_LIMIT:
                raise ValueError('file_too_large')
            with os.fdopen(fd, 'rb', closefd=False) as f:
                data = f.read(FILE_LIMIT + 1)
            if len(data) > FILE_LIMIT:
                raise ValueError('file_too_large')
            return {'path': args['path'], 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                    'data': base64.b64encode(data).decode()}
        offset, limit = args['offset'], args['limit']
        os.lseek(fd, offset, os.SEEK_SET)
        data = os.read(fd, limit)
        return {'path': args['path'], 'content': data.decode('utf-8', 'replace'),
                'offset': offset, 'next_offset': offset + len(data)}
    finally:
        os.close(fd)


def setup(args):
    # No user-controlled shell fragments are evaluated here.
    CONTROL.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(CONTROL, 0o700)
    for tool in ('iptables', 'ip6tables'):
        subprocess.run([tool, '-P', 'OUTPUT', 'DROP'], check=True, capture_output=True)
        subprocess.run([tool, '-F', 'OUTPUT'], check=True, capture_output=True)
    def rule(*parts):
        subprocess.run(['iptables', '-A', 'OUTPUT', *parts], check=True, capture_output=True)
    # Docker's resolver has a NATed port. Query its loopback address only for DNS.
    # raw/OUTPUT runs BEFORE Docker's DNS DNAT and prevents arbitrary loopback
    # access (including directly addressing its randomized resolver port).
    subprocess.run(['iptables', '-t', 'raw', '-N', 'P2R_LOCAL'], check=True, capture_output=True)
    subprocess.run(['iptables', '-t', 'raw', '-A', 'OUTPUT', '-p', 'icmp', '--icmp-type', 'destination-unreachable', '-j', 'ACCEPT'], check=True, capture_output=True)
    subprocess.run(['iptables', '-t', 'raw', '-A', 'OUTPUT', '-s', '127.0.0.11', '-m', 'owner', '--uid-owner', '0', '-j', 'ACCEPT'], check=True, capture_output=True)
    subprocess.run(['iptables', '-t', 'raw', '-A', 'OUTPUT', '-d', '127.0.0.0/8', '-j', 'P2R_LOCAL'], check=True, capture_output=True)
    for proto in ('udp', 'tcp'):
        subprocess.run(['iptables', '-t', 'raw', '-A', 'P2R_LOCAL', '-d', '127.0.0.11', '-p', proto, '--dport', '53', '-j', 'RETURN'], check=True, capture_output=True)
    subprocess.run(['iptables', '-t', 'raw', '-A', 'P2R_LOCAL', '-j', 'DROP'], check=True, capture_output=True)
    # Kernel-generated rejection replies and Docker resolver replies must reach
    # local sockets. The experiment UID has no CAP_NET_RAW to forge these.
    rule('-p', 'icmp', '--icmp-type', 'destination-unreachable', '-j', 'ACCEPT')
    rule('-s', '127.0.0.11', '-m', 'owner', '--uid-owner', '0', '-j', 'ACCEPT')
    # After Docker's DNS DNAT, allow resolver sockets but no other loopback.
    rule('-d', '127.0.0.11', '-p', 'udp', '-j', 'ACCEPT')
    rule('-d', '127.0.0.11', '-p', 'tcp', '-j', 'ACCEPT')
    private = ['0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
               '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24',
               '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24',
               '224.0.0.0/4', '240.0.0.0/4']
    private += args['host_addresses']
    for network in private:
        rule('-d', network, '-j', 'REJECT', '--reject-with', 'icmp-admin-prohibited')
    if args['public_internet']:
        rule('-m', 'owner', '--uid-owner', '10001', '-j', 'ACCEPT')
        rule('-m', 'owner', '--uid-owner', '20000-60000', '-j', 'ACCEPT')
    # IPv6 stays entirely fail-closed, including IPv4-mapped addresses.
    # Root holds CAP_CHOWN but not CAP_FOWNER: after the chown only the experiment user may
    # change modes under /work (Docker Desktop's Windows bind mount enforces this).
    os.chown('/work', 10001, 10001)
    agent = ['setpriv', '--reuid=10001', '--regid=10001', '--clear-groups',
             '--bounding-set=-all', '--inh-caps=-all', '--ambient-caps=-all', '--no-new-privs']
    subprocess.run([*agent, 'chmod', '2775', '/work'], check=True, capture_output=True)
    subprocess.run([*agent, 'python3', '-I', '-m', 'venv', '/work/.venv'], check=True, capture_output=True)
    subprocess.run([*agent, 'chmod', '-R', 'g+rwX', '/work/.venv'], check=True, capture_output=True)
    return {'setup': True, 'ipv6': 'denied'}


def network_probe(args):
    import errno
    import urllib.request
    public = False
    errors = {}
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('https://example.com', timeout=12) as response:
            public = response.status == 200
    except Exception as exc:
        errors['public'] = repr(exc)
    checks = {}
    # Only EACCES/EPERM count as a proven IPv4 policy denial; refused TCP
    # connections or timeouts never establish IPv4 isolation.
    hosts = ['10.0.0.1', '169.254.169.254', '172.16.0.1', '192.168.0.1', *args['host_addresses']]
    for host in [h for h in hosts if not h.startswith('127.') ]:
        try:
            s = socket.create_connection((host, 80), timeout=2)
            s.close()
            checks[host] = False
        except OSError as exc:
            errors[host] = repr(exc)
            checks[host] = exc.errno in (errno.EACCES, errno.EPERM, errno.EHOSTUNREACH)
    # This listener proves loopback is blocked even when a service is present.
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    try:
        s = socket.create_connection(listener.getsockname(), timeout=.25)
        s.close()
        checks['loopback_listener'] = False
    except OSError:
        checks['loopback_listener'] = True
    finally:
        listener.close()
    try:
        s = socket.socket(socket.AF_INET6)
        s.settimeout(.25)
        s.connect(('fd00::1', 80))
        checks['ipv6_private'] = False
        s.close()
    except OSError:
        checks['ipv6_private'] = True
    status_text = Path('/proc/self/status').read_text()
    caps_zero = 'CapEff:\t0000000000000000' in status_text
    return {'public': public, 'private_denied': all(checks.values()), 'checks': checks, 'errors': errors,
            'uid': os.getuid(), 'caps_zero': caps_zero, 'no_new_privileges': 'NoNewPrivs:\t1' in status_text}


def main(op, args):
    if op == 'guardian':
        guardian(args['job_id'], args['uid'])
        return
    if op == 'worker':
        run_worker(args['job_id'], args['uid'], args['timeout_seconds'])
        return
    if op == 'setup':
        return setup(args)
    if op == 'network_probe':
        return network_probe(args)
    if op in ('read', 'write', 'collect'):
        return file_op(op, args)
    if op == 'resources':
        total, partial, stop_at = 0, False, time.monotonic() + 60
        for root, dirs, files in os.walk('/work', followlinks=False):
            if time.monotonic() > stop_at:  # an observation, not enforcement: report what was counted
                partial = True
                break
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
            for name in files:
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except FileNotFoundError:
                    pass
        gpu = subprocess.run(['sh', '-c', 'command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=index,name --format=csv,noheader'], capture_output=True, text=True)
        return {'storage_bytes_used': total, 'storage_count_partial': partial, 'storage_enforcement': 'observed', 'gpu_observation': gpu.stdout.strip(), 'gpu_exit_code': gpu.returncode}
    job = args['job_id']
    directory = job_path(job)
    if op == 'start':
        with (CONTROL / 'launch.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            fresh = not directory.exists()
            if fresh:
                directory.mkdir(mode=0o700)
                counter = CONTROL / 'next_uid'
                uid = int(counter.read_text()) if counter.exists() else 20000
                if uid > 60000:
                    raise ValueError('job uid capacity exhausted; explicitly recreate target')
                counter.write_text(str(uid + 1))
                args['uid'] = uid
                save(directory / 'spec.json', args)
            else:
                old = json.loads((directory / 'spec.json').read_text())
                if old['command'] != args['command'] or old['timeout_seconds'] != args['timeout_seconds']:
                    raise ValueError('launch_conflict')
            if fresh:
                # Durable reservation precedes spawn. A crash in this tiny window
                # leaves dispatching/unknown, never an automatic second launch.
                save(directory / 'state.json', {'job_id': job, 'state': 'dispatching',
                                               'exit_code': None, 'offset': 0})
                process = subprocess.Popen([sys.executable, __file__, 'worker', json.dumps({'job_id': job, 'uid': args['uid'], 'timeout_seconds': args['timeout_seconds']})],
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, start_new_session=True)
                until = time.monotonic() + 5
                while json.loads((directory / 'state.json').read_text()).get('state') == 'dispatching' and time.monotonic() < until:
                    if process.poll() is not None:
                        raise ValueError('supervisor_failed')
                    time.sleep(.01)
                if not (directory / 'state.json').exists():
                    raise ValueError('supervisor_start_timeout')
            result = status(job)
            result['deduplicated'] = not fresh
            return result
    if op == 'status':
        return status(job)
    if op == 'stop':
        state = status(job)
        if state['state'] in ('running', 'starting'):
            (directory / 'cancel').touch()
            until = time.monotonic() + 8
            while time.monotonic() < until:
                state = status(job)
                if state['state'] not in ('running', 'starting'):
                    return state
                time.sleep(.05)
            raise ValueError('stop_unconfirmed')
        return state
    if op in ('logs', 'result'):
        result = status(job)
        if op == 'logs':
            path = directory / 'log'
            offset = args['offset']
            if path.exists():
                with path.open('rb') as f:
                    f.seek(offset)
                    chunk = f.read(args['limit'])
            else:
                chunk = b''
            result.update(stdout=chunk.decode('utf-8', 'replace'), stderr='', offset=offset, next_offset=offset + len(chunk))
        else:
            for name in ('stdout', 'stderr'):
                path = directory / name
                result[name] = path.read_bytes()[:LIMIT].decode('utf-8', 'replace') if path.exists() else ''
        return result
    raise ValueError('unknown operation')


if __name__ == '__main__':
    try:
        value = main(sys.argv[1], json.loads(sys.argv[2]))
        if value is not None:
            emit(value)
    except Exception as exc:
        emit({'error': str(exc), 'code': 'container_error'})
        sys.exit(1)
