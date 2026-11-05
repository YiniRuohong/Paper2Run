"""Deterministic worker/guardian interleavings; no processes or Docker required."""
import ctypes
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from p2r_sandbox.autonomous import _container as controller


@pytest.fixture
def worker(tmp_path, monkeypatch):
    job = 'a' * 64
    uid = 20000
    directory = tmp_path / job
    directory.mkdir()
    (directory / 'spec.json').write_text(json.dumps({
        'command': 'test command', 'timeout_seconds': 10,
    }))
    monkeypatch.setattr(controller, 'CONTROL', tmp_path)
    monkeypatch.setattr(ctypes, 'CDLL', lambda *a, **kw: SimpleNamespace(prctl=lambda *a: 0))
    monkeypatch.setattr(controller, 'boot', lambda: 'test-boot')
    monkeypatch.setattr(controller, 'identity', lambda pid: str(pid))
    monkeypatch.setattr(controller.os, 'chown', lambda *a: None)
    monkeypatch.setattr(controller.os, 'umask', lambda *a: None)
    monkeypatch.setattr(controller.os, 'set_blocking', lambda *a: None)
    # Redirect the worker's per-job HOME into pytest's temporary directory.
    monkeypatch.setattr(controller, 'Path', lambda path: (
        tmp_path / 'home' if path == f'/tmp/p2r-job-{uid}' else Path(path)))
    clock = SimpleNamespace(monotonic=0, epoch=100)
    monkeypatch.setattr(controller.time, 'monotonic', lambda: clock.monotonic)
    monkeypatch.setattr(controller.time, 'time', lambda: clock.epoch)
    child = SimpleNamespace(pid=123, stdout=io.BytesIO(), stderr=io.BytesIO(), returncode=None)
    child.stdout.fileno = lambda: 101
    child.stderr.fileno = lambda: 102
    child.poll = lambda: child.returncode
    child.wait = lambda: child.returncode
    launches = []

    def popen(*args, **kwargs):
        launches.append(args)
        return SimpleNamespace() if len(launches) == 1 else child

    monkeypatch.setattr(controller.subprocess, 'Popen', popen)
    scenario = SimpleNamespace(directory=directory, clock=clock, child=child,
                               on_select=lambda: None, on_cleanup=lambda: None,
                               clean=True, kills=[], observed=[])

    def kill_uid(actual_uid):
        assert actual_uid == uid
        scenario.kills.append(actual_uid)
        child.returncode = -15
        scenario.on_cleanup()
        return scenario.clean

    monkeypatch.setattr(controller, 'kill_uid', kill_uid)

    class Selector:
        def __init__(self):
            self.keys = {}

        def register(self, stream, events, name):
            self.keys[stream] = SimpleNamespace(fileobj=stream, data=name)

        def get_map(self):
            return self.keys

        def select(self, timeout):
            scenario.on_select()
            child.returncode = 0 if child.returncode is None else child.returncode
            return [(key, 1) for key in self.keys.values()]

        def unregister(self, stream):
            del self.keys[stream]

        def close(self):
            pass

    monkeypatch.setattr(controller.selectors, 'DefaultSelector', Selector)
    monkeypatch.setattr(controller.os, 'read', lambda *a: b'')

    def guardian():
        controller.guardian(job, uid)
        scenario.observed.append(controller.status(job)['state'])

    def run():
        controller.run_worker(job, uid, 10)
        assert len(launches) == 2
        return json.loads((directory / 'state.json').read_text())

    scenario.guardian = guardian
    scenario.run = run
    return scenario


@pytest.mark.parametrize('guardian_clean', [True, False])
def test_guardian_cancel_during_selector_drain_is_not_overwritten(worker, guardian_clean):
    def cancel_and_guard():
        (worker.directory / 'cancel').touch()
        worker.clean = guardian_clean
        worker.guardian()
        # The worker's later cleanup can succeed even after guardian failed.
        worker.clean = True

    worker.on_select = cancel_and_guard
    result = worker.run()
    expected = 'stopped' if guardian_clean else 'stop_failed'
    assert worker.observed == [expected]
    assert result['state'] == expected
    assert result['exit_code'] == -15
    assert result['offset'] == 0
    assert len(worker.kills) == 2


def test_guardian_timeout_during_selector_drain_is_not_overwritten(worker):
    def expire_and_guard():
        worker.clock.epoch = 111
        worker.guardian()

    worker.on_select = expire_and_guard
    result = worker.run()
    assert worker.observed == ['timeout']
    assert result['state'] == 'timeout'


@pytest.mark.parametrize('clean', [True, False])
def test_cancel_during_final_cleanup(worker, clean):
    worker.on_cleanup = lambda: (worker.directory / 'cancel').touch()
    worker.clean = clean
    assert worker.run()['state'] == ('stopped' if clean else 'stop_failed')


@pytest.mark.parametrize('clean', [True, False])
def test_worker_timeout_is_not_changed_by_later_cancel(worker, monkeypatch, clean):
    # Expire after computing the worker deadline, before its first loop check.
    ticks = iter([0, 11])
    monkeypatch.setattr(controller.time, 'monotonic', lambda: next(ticks, 11))
    worker.on_cleanup = lambda: (worker.directory / 'cancel').touch()
    worker.clean = clean
    assert worker.run()['state'] == ('timeout' if clean else 'stop_failed')


@pytest.mark.parametrize('reason', ['stopped', 'timeout'])
@pytest.mark.parametrize('clean', [True, False])
def test_guardian_finishing_last_preserves_worker_results(worker, reason, clean):
    def finish_worker_during_guardian_cleanup():
        controller.finish(worker.directory / 'state.json', 'exited',
                          exit_code=-15, offset=7, output_truncated=False)

    def guard_after_worker_check():
        if reason == 'stopped':
            (worker.directory / 'cancel').touch()
        else:
            worker.clock.epoch = 111
        worker.on_cleanup = finish_worker_during_guardian_cleanup
        worker.clean = clean
        worker.guardian()
        state = json.loads((worker.directory / 'state.json').read_text())
        assert state['state'] == (reason if clean else 'stop_failed')
        assert state['exit_code'] == -15
        assert state['offset'] == 7
        assert state['output_truncated'] is False
        worker.on_cleanup = lambda: None
        worker.clean = True

    worker.on_select = guard_after_worker_check
    assert worker.run()['state'] == (reason if clean else 'stop_failed')


@pytest.mark.parametrize('clean', [True, False])
def test_normal_completion_and_cleanup_failure(worker, clean):
    worker.clean = clean
    result = worker.run()
    assert result['state'] == ('exited' if clean else 'stop_failed')
    assert result['exit_code'] == 0
