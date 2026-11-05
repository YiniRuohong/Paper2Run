"""Actual OpenSSH transport to local sshd, not a GPU-host execution claim."""
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from p2r_sandbox.autonomous import ExecutionError, ExecutionTarget

pytestmark = pytest.mark.skipif(os.environ.get('P2R_SSH_TESTS') != '1', reason='opt-in local sshd transport test')


def run(*args):
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout


def test_actual_strict_ssh_transport(tmp_path):
    # Test-generated key and manually copied known-host public key live only in
    # the controller test folder. No Docker socket or private key enters sshd.
    key = tmp_path / 'key'
    run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key))
    name = 'p2r-sshd-test-' + uuid.uuid4().hex[:10]
    try:
        run('docker', 'run', '-d', '--name', name, '-p', '127.0.0.1::22', 'paper2run-sshd-test:1')
        port = run('docker', 'port', name, '22/tcp').strip().rsplit(':', 1)[1]
        run('docker', 'exec', name, 'sh', '-c', 'mkdir -p /root/.ssh; chmod 700 /root/.ssh')
        subprocess.run(['docker', 'exec', '-i', name, 'sh', '-c', 'cat > /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys'], input=key.with_suffix('.pub').read_text(), text=True, check=True)
        host_key = run('docker', 'exec', name, 'cat', '/etc/ssh/ssh_host_ed25519_key.pub').strip()
        known = tmp_path / 'known_hosts'
        known.write_text(f'[127.0.0.1]:{port} {host_key}\n')
        config = tmp_path / 'config'
        config.write_text(f'Host local-test\n HostName 127.0.0.1\n Port {port}\n User root\n IdentityFile {key}\n UserKnownHostsFile {known}\n IdentitiesOnly yes\n')
        obj = ExecutionTarget({'kind': 'ssh', 'ssh_alias': 'local-test', 'ssh_config': str(config),
                               'workspace': '/work/experiment', 'network': {'public_internet': True, 'host_and_metadata_access': False}}, tmp_path / 'state')
        result = obj._host(['python3', '-c', 'import json; print(json.dumps({"transport":"actual-ssh", "arg":"a; b"}))'])
        assert 'actual-ssh' in result.stdout
        assert 'a; b' in result.stdout
        known.write_text('')
        with pytest.raises(ExecutionError) as error:
            obj._host(['true'])
        assert error.value.code == 'ssh_host_key'
        obj.close()
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
