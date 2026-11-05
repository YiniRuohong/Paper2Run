"""Real Docker integration. No mocks and no remote customer machines.

Run: P2R_DOCKER_TESTS=1 uv run --extra test python -m pytest -q tests/integration/autonomous/test_execution_docker.py
"""
import concurrent.futures
import os
import subprocess
import time
from pathlib import Path

import pytest

from p2r_sandbox.autonomous import ExecutionError, ExecutionTarget

pytestmark = pytest.mark.skipif(os.environ.get('P2R_DOCKER_TESTS') != '1', reason='opt-in real Docker tests')


@pytest.fixture(scope='module')
def target(tmp_path_factory):
    root = tmp_path_factory.mktemp('execution')
    config = {'kind': 'local', 'workspace': str(root / 'work'), 'image': 'paper2run-experiment:1',
              'gpu_devices': [], 'storage_bytes': 100000000,
              'network': {'public_internet': True, 'host_and_metadata_access': False}}
    obj = ExecutionTarget(config, root / 'state')
    try:
        report = obj.prepare()
        # Keep this fixture deterministic if VPN/virtual adapter addresses flap
        # during unrelated lifecycle assertions. Drift is tested separately.
        pinned_addresses = obj._addresses()
        obj._host_addresses = lambda: list(pinned_addresses)
        assert report['network']['public']
        assert report['network']['private_denied']
        assert report['network']['uid'] == 10001
        assert report['network']['caps_zero']
        assert report['storage_enforcement'] == 'observed'
        yield obj
    finally:
        obj.close()
        subprocess.run(['docker', 'rm', '-f', obj.container], capture_output=True)
        subprocess.run(['docker', 'network', 'rm', obj.container + '-net'], capture_output=True)


def wait(target, job, seconds=12):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        result = target.status(job)
        if result['state'] not in ('running', 'starting', 'pending'):
            return result
        time.sleep(.1)
    pytest.fail('job never terminated')


def test_execution_actual_output_and_venv(target):
    result = target.execute("echo hello; echo error >&2; exit 7")
    assert result['exit_code'] == 7
    assert result['stdout'] == 'hello\n'
    assert result['stderr'] == 'error\n'
    result = target.execute("python -c 'import sys; print(sys.prefix)' && pip --version")
    assert result['exit_code'] == 0
    assert '/work/.venv' in result['stdout']
    assert target.execute('iptables -F')['exit_code'] != 0
    assert target.execute('test ! -S /var/run/docker.sock && test ! -r /var/lib/p2r-control/launch.lock')['exit_code'] == 0


def test_file_protection_and_collect(target, tmp_path):
    target.write('sub/hello.txt', 'héllo')
    assert target.read('sub/hello.txt')['content'] == 'héllo'
    result = target.collect('sub/hello.txt', tmp_path / 'copy')
    assert result['bytes'] == 6
    assert (tmp_path / 'copy').read_text() == 'héllo'
    target.execute('ln -sf /etc/passwd escape; ln -sf /tmp outdir')
    for path in ('../etc/passwd', '/etc/passwd', 'escape', 'outdir/file'):
        with pytest.raises(ExecutionError):
            target.write(path, 'bad')
    with pytest.raises(ExecutionError):
        target.read('escape')


def test_background_resume_dedup_and_logs(target):
    command = 'echo begin; sleep 2; echo end >> counter; echo done'
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: target.start(command, 'same-launch', 10), range(2)))
    assert results[0]['job_id'] == results[1]['job_id']
    assert sum(not x['deduplicated'] for x in results) == 1
    job = results[0]['job_id']
    target.close()
    # The same object models an open application session; close/reopen semantics
    # are exercised separately where reconnect deliberately re-probes addresses.
    reopened = target
    assert wait(reopened, job)['exit_code'] == 0
    assert reopened.read('counter')['content'] == 'end\n'
    first = reopened.logs(job, limit=3)
    second = reopened.logs(job, offset=first['next_offset'])
    assert first['stdout'] + second['stdout'] == 'begin\ndone\n'
    assert reopened.start(command, 'same-launch', 10)['deduplicated']
    with pytest.raises(ExecutionError, match='launch_conflict'):
        reopened.start('echo changed', 'same-launch', 10)


def test_timeout_and_cancel_tree(target):
    job = target.start('sleep 60 & sleep 60 & wait', 'timeout', 1)['job_id']
    assert wait(target, job)['state'] == 'timeout'
    job = target.start('setsid sleep 60 & sleep 60 & wait', 'cancel', 60)['job_id']
    time.sleep(.3)
    assert target.stop(job)['state'] == 'stopped'
    result = target.execute("ps -eo args | grep '[s]leep 60' || true")
    assert result['stdout'] == ''


def test_output_is_bounded(target):
    result = target.execute("python -c 'import sys; sys.stdout.write(\"x\"*2000000); sys.stderr.write(\"y\"*2000000)'", 10)
    assert result['exit_code'] == 0
    assert len(result['stdout']) + len(result['stderr']) <= 1000000
    assert result['output_truncated']
    assert len(target.logs(result['job_id'], limit=10000000)['stdout']) == 1000000


def test_private_and_redirect_denied(target):
    result = target.execute("curl --noproxy '*' -fsS --max-time 10 https://example.com >/dev/null")
    assert result['exit_code'] == 0
    for host in ('169.254.169.254', '10.0.0.1', '127.0.0.1', '[::1]'):
        assert target.execute(f"curl --noproxy '*' -fsS --max-time 2 http://{host}/")['exit_code'] != 0
    # Observe the actual 302/Location before following it. An unavailable public
    # service is explicitly skipped, never mistaken for a private-network denial.
    redirect = 'https://httpbin.org/redirect-to?url=http%3A%2F%2F169.254.169.254%2F'
    first = target.execute(f"curl --noproxy '*' -sS -D - -o /dev/null --max-time 15 '{redirect}'", 20)
    if first['exit_code'] != 0 or '302' not in first['stdout'] or 'location: http://169.254.169.254/' not in first['stdout'].lower():
        pytest.skip('Public redirect service did not supply observable HTTP 302/Location: ' + repr(first))
    result = target.execute(f"curl --noproxy '*' -L -v --max-time 15 '{redirect}'", 20)
    assert result['exit_code'] != 0
    assert '302' in result['stderr']
    assert '169.254.169.254' in result['stderr']
    assert 'No route to host' in result['stderr'] or 'Permission denied' in result['stderr']


def test_reconnect_refuses_changed_address_snapshot(target):
    import json
    # Real interface enumeration and Docker transport, with a deliberately stale
    # saved identity record; no host network interfaces are modified by this test.
    record = target.state_dir / (target.container + '.json')
    original = record.read_text()
    data = json.loads(original)
    # Record the live target enumeration; interfaces may change during tests
    # (VPN/virtual adapters), so only the deliberate stale addition is asserted.
    live_addresses = target._host_addresses()
    data['host_addresses'] = sorted(set(live_addresses))
    record.write_text(json.dumps(data))
    original_current = target._current_addresses
    target._current_addresses = lambda obj: sorted(set(live_addresses) | {'8.8.4.4'})
    try:
        # Same live object must detect drift too; no close/reopen cache reset.
        with pytest.raises(ExecutionError) as error:
            target.execute('echo must-not-run')
        assert error.value.code == 'host_addresses_changed'
        assert not target._prepared
    finally:
        target._current_addresses = original_current
        record.write_text(original)
    assert target.prepare()['ready']


def test_removed_policy_covered_address_does_not_block(target):
    # OrbStack/Docker can remove a host adapter while the container policy still
    # rejects the encompassing private network. Removal cannot weaken isolation.
    import json
    record = target.state_dir / (target.container + '.json')
    original = record.read_text()
    saved = json.loads(original)
    removed = next((address for address in saved['host_addresses'] if address.startswith('192.168.')), None)
    if removed is None:
        pytest.skip('No private IPv4 adapter available for removal regression')
    original_current = target._current_addresses
    try:
        saved['host_addresses'].remove(removed)
        record.write_text(json.dumps(saved))
        current = json.loads(original)['host_addresses']
        target._current_addresses = lambda obj: [address for address in current if address != removed]
        assert target.execute('echo address-removal-safe')['stdout'] == 'address-removal-safe\n'
    finally:
        target._current_addresses = original_current
        record.write_text(original)


def test_supervisor_loss_cleans_reparented_children_only(target):
    other = target.start('sleep 40', 'unrelated-survivor', 45)['job_id']
    job = target.start('setsid sleep 60 & echo $! > escaped.pid; wait', 'lost-supervisor', 3)['job_id']
    time.sleep(.5)
    state = target.status(job)
    subprocess.run(['docker', 'exec', '-u', '0', target.container, 'kill', '-9', str(state['supervisor_pid'])], check=True)
    # No status query drives recovery: guardian must act without the client.
    time.sleep(4)
    code = f'import pathlib; p=pathlib.Path("/var/lib/p2r-control/{job}/state.json"); print(p.read_text())'
    import json
    saved = json.loads(subprocess.run(['docker', 'exec', '-u', '0', target.container, 'python3', '-c', code], check=True, capture_output=True, text=True).stdout)
    assert saved['state'] == 'interrupted'
    check = f'import sys;sys.path.insert(0,"/opt");import importlib.util;s=importlib.util.spec_from_file_location("c","/opt/p2r-controller.py");m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(m.uid_members({state["job_uid"]}))'
    assert subprocess.run(['docker', 'exec', '-u', '0', target.container, 'python3', '-c', check], capture_output=True, text=True, check=True).stdout.strip() == '[]'
    assert target.status(other)['state'] == 'running'
    assert target.stop(other)['state'] == 'stopped'


def test_lost_launch_response_reconciles_new_target(target):
    import hashlib
    command = 'echo once >> lost-response; sleep 2'
    # Real remote controller commits the launch; intentionally discard response.
    job = hashlib.sha256(b'lost-response').hexdigest()
    target._ready()
    target._call('start', {'command': command, 'job_id': job, 'timeout_seconds': 10})
    reopened = ExecutionTarget(target.config, target.state_dir)
    reopened._host_addresses = target._host_addresses
    result = reopened.start(command, 'lost-response', 10)
    assert result['deduplicated']
    assert wait(reopened, job)['exit_code'] == 0
    assert reopened.read('lost-response')['content'] == 'once\n'


def test_same_object_rejects_firewall_tampering(target):
    saved = target._docker('exec', '-u', '0', target.container, 'iptables-save', '-c').stdout
    try:
        target._docker('exec', '-u', '0', target.container, 'iptables', '-I', 'OUTPUT', '1', '-j', 'ACCEPT')
        with pytest.raises(ExecutionError) as error:
            target.execute('echo bypass > firewall-bypass')
        assert error.value.code == 'isolation_changed'
        assert not (Path(target.workspace) / 'firewall-bypass').exists()
    finally:
        target._docker('exec', '-i', '-u', '0', target.container, 'iptables-restore', '-c', input=saved)
    assert target.prepare()['ready']


def test_container_restart_is_not_running(target):
    job = target.start('sleep 60', 'reboot', 60)['job_id']
    time.sleep(.2)
    subprocess.run(['docker', 'restart', '-t', '1', target.container], check=True, capture_output=True)
    target.close()
    # Test restart reporting directly: status must remain queryable even while
    # new execution would require renewed host-address/network authorization.
    reopened = ExecutionTarget(target.config, target.state_dir)
    assert reopened.status(job)['state'] == 'interrupted'
    with pytest.raises(ExecutionError) as error:
        target.start('echo forbidden > bypass', 'restart-bypass', 10)
    assert error.value.code == 'isolation_changed'
    assert not (Path(target.workspace) / 'bypass').exists()
