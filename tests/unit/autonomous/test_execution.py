import subprocess

import pytest

from p2r_sandbox.autonomous import ExecutionError, ExecutionTarget


def config(tmp_path, **kwargs):
    return {'kind': 'local', 'workspace': str(tmp_path / 'work'),
            'image': 'paper2run-experiment:1', 'gpu_devices': [], 'storage_bytes': 10000,
            'network': {'public_internet': True, 'host_and_metadata_access': False}, **kwargs}


def test_config_and_ranges(tmp_path):
    with pytest.raises(ExecutionError):
        ExecutionTarget(config(tmp_path, workspace='/'), tmp_path / 'state')
    with pytest.raises(ExecutionError):
        ExecutionTarget(config(tmp_path, network={'public_internet': True, 'host_and_metadata_access': True}), tmp_path / 'state')
    with pytest.raises(ExecutionError):
        ExecutionTarget(config(tmp_path), tmp_path / 'work' / 'state')
    obj = ExecutionTarget(config(tmp_path), tmp_path / 'state')
    assert obj._range(0, 2000000)['limit'] == 1000000
    with pytest.raises(ExecutionError):
        obj._range(-1, 10)
    with pytest.raises(ExecutionError):
        obj.start('true', 'test', 0)


@pytest.mark.parametrize('message,code', [
    ('Host key verification failed.', 'ssh_host_key'),
    ('WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!', 'ssh_host_key'),
    ('Permission denied (publickey).', 'ssh_authentication'),
])
def test_ssh_errors_are_strict(tmp_path, monkeypatch, message, code):
    obj = ExecutionTarget(config(tmp_path, kind='ssh', ssh_alias='test-only'), tmp_path / 'state')
    def fake(argv, **kwargs):
        assert 'StrictHostKeyChecking=yes' in argv
        assert 'BatchMode=yes' in argv
        assert 'ForwardAgent=no' in argv
        assert 'ClearAllForwardings=yes' in argv
        assert '-A' not in argv
        return subprocess.CompletedProcess(argv, 255, '', message)
    monkeypatch.setattr(subprocess, 'run', fake)
    with pytest.raises(ExecutionError) as error:
        obj.probe()
    assert error.value.code == code


def test_stable_identity_close_and_no_other_packages(tmp_path):
    obj = ExecutionTarget(config(tmp_path), tmp_path / 'state')
    second = ExecutionTarget(config(tmp_path), tmp_path / 'state')
    assert obj.container == second.container
    obj.close()  # deliberately no external side effects
    assert ExecutionError('example', 'example').as_dict() == {'message': 'example', 'code': 'example'}


@pytest.mark.parametrize('platform', ['linux', 'darwin'])
def test_multihomed_sockaddr_enumeration(platform):
    import socket
    import sys
    from p2r_sandbox.autonomous._interfaces import ipv4_addresses
    def address(ip):
        family = bytes([16, socket.AF_INET]) if platform == 'darwin' else socket.AF_INET.to_bytes(2, sys.byteorder)
        return family + b'\x00\x00' + socket.inet_aton(ip)
    records = [address('127.0.0.1'), address('10.1.0.2'),
               address('8.8.4.4'), address('9.9.9.9'), address('8.8.4.4'), b'']
    assert ipv4_addresses(records, platform) == ['10.1.0.2', '127.0.0.1', '8.8.4.4', '9.9.9.9']


def test_policy_reject_parser_requires_unchanged_policy(tmp_path, monkeypatch):
    target = ExecutionTarget(config(tmp_path), tmp_path / 'state')
    record = target.state_dir / (target.container + '.json')
    record.write_text('{"identity": {"policy": "fingerprint"}}')
    rules = """*filter
:OUTPUT DROP [0:0]
-A OUTPUT -m owner --uid-owner 10001 -j ACCEPT
-A OUTPUT -d 192.168.0.0/16 -j REJECT --reject-with icmp-admin-prohibited
COMMIT
"""
    monkeypatch.setattr(target, '_policy_fingerprint', lambda: 'fingerprint')
    monkeypatch.setattr(target, '_docker', lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, rules, ''))
    covered = target._policy_reject_networks()
    assert any('192.168.0.0/16' == str(network) for network in covered)

    monkeypatch.setattr(target, '_docker', lambda *args, **kwargs: subprocess.CompletedProcess(
        args, 0, rules.replace('-m owner --uid-owner 10001 ', ''), ''))
    assert target._policy_reject_networks() == []


def test_real_target_interfaces_enumerated(tmp_path):
    from p2r_sandbox.autonomous._interfaces import interface_addresses
    target = ExecutionTarget(config(tmp_path), tmp_path / 'state')
    assert target._host_addresses() == interface_addresses()
    assert '127.0.0.1' in target._host_addresses()
