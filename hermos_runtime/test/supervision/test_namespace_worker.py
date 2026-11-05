"""Host-only namespace lifecycle tests; privileged operations fail closed."""

import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hermos_runtime.supervision import namespace_worker as worker


@pytest.fixture(autouse=True)
def block_privileged_or_unowned_operations(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail('unmocked privileged operation or process operation')

    monkeypatch.setattr(worker, 'Group', blocked)
    monkeypatch.setattr(ctypes, 'CDLL', blocked)
    for name in ('Popen', 'run', 'check_output'):
        monkeypatch.setattr(subprocess, name, blocked)
    for name in ('setgroups', 'setresgid', 'setresuid', 'setgid', 'setuid',
                 'setegid', 'seteuid', 'waitpid', 'pidfd_open', 'kill', 'fork'):
        monkeypatch.setattr(os, name, blocked)
    monkeypatch.setattr(signal, 'pidfd_send_signal', blocked)


def unprivileged_main(monkeypatch, tmp_path, problem=None):
    stage = tmp_path / 'worker-stage'
    stage.mkdir()
    isolation = {'uid': 1000, 'gid': 1000, 'groups': [44, 1000],
                 'after': {'pid': 'pid', 'ipc': 'ipc', 'mnt': 'mnt'}, 'shm_device': 2}
    calls = []
    monkeypatch.setattr(os, 'getuid', lambda: 0 if problem == 'root' else 1000)
    monkeypatch.setattr(os, 'getgid', lambda: 1000)
    monkeypatch.setattr(os, 'getresuid', lambda: (1000, 1000, 0) if problem == 'saved_uid' else (1000,) * 3)
    monkeypatch.setattr(os, 'getresgid', lambda: (1000,) * 3)
    monkeypatch.setattr(os, 'getgroups', lambda: [0] if problem == 'groups' else [44, 1000])
    monkeypatch.setattr(worker, 'namespaces', lambda: {} if problem == 'namespace' else isolation['after'])
    monkeypatch.setattr(worker, 'shm_inventory', lambda: {'unexpected': {}} if problem == 'inventory' else {})
    monkeypatch.setattr(worker, 'process_start_time', lambda pid: '1234')
    read_text, stat = Path.read_text, os.stat
    def contents(path, *args, **kwargs):
        if str(path) == '/proc/self/status':
            return ('CapInh:\t0\nCapPrm:\t0\nCapAmb:\t0\n'
                    f'CapEff:\t{int(problem == "capability")}\n'
                    f'NoNewPrivs:\t{int(problem != "no_new_privs")}\n')
        return read_text(path, *args, **kwargs)
    def information(path, *args, **kwargs):
        if str(path) == '/dev/shm':
            return SimpleNamespace(st_dev=1 if problem == 'device' else 2)
        return stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', contents)
    monkeypatch.setattr(os, 'stat', information)
    monkeypatch.setattr(sys, 'argv', ['namespace_worker.py', str(stage), json.dumps(isolation)])
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(fileno=lambda: 98765))
    request = {'startup_timeout': 1}
    class Control:
        def __init__(self):
            self.messages = iter([request, {'ack': True}])
        def read(self, timeout):
            calls.append('read')
            return next(self.messages)
    monkeypatch.setattr(worker, 'JsonInput', lambda fd: Control())
    def run_epoch(received, path, record, control):
        assert received == request and path == stage
        calls.append('peers')
        record.update(teardown_complete=True, echild=True)
    monkeypatch.setattr(worker, 'run_epoch', run_epoch)
    return stage, calls


def test_nonroot_worker_verifies_bootstrap_handoff_before_ack_and_peers(monkeypatch, tmp_path):
    stage, calls = unprivileged_main(monkeypatch, tmp_path)
    assert worker.main() == 0
    assert calls == ['read', 'read', 'peers']
    assert json.loads((stage / 'worker.json').read_text())['isolation']['uid'] == 1000
    assert json.loads((stage / 'worker-result.json').read_text())['teardown_complete']


@pytest.mark.parametrize('problem', ['root', 'saved_uid', 'groups', 'namespace', 'inventory',
                                      'capability', 'no_new_privs', 'device'])
def test_worker_rejects_invalid_handoff_before_request_or_evidence(monkeypatch, tmp_path, problem):
    stage, calls = unprivileged_main(monkeypatch, tmp_path, problem)
    assert worker.main() == 70
    assert not calls and not (stage / 'worker.json').exists()
    assert not (stage / 'worker-result.json').exists()


def test_two_json_commands_in_one_pipe_write_are_both_consumed():
    reader, writer = os.pipe()
    try:
        os.write(writer, b'{"peer": 0, "stdin": "finish"}\n{"stop": true}\n')
        control = worker.JsonInput(reader)
        assert control.read(0.1) == {'peer': 0, 'stdin': 'finish'}
        assert control.read(0.1) == {'stop': True}
    finally:
        os.close(reader)
        os.close(writer)


def test_json_input_distinguishes_timeout_from_parent_eof():
    reader, writer = os.pipe()
    try:
        control = worker.JsonInput(reader)
        with pytest.raises(TimeoutError):
            control.read(0)
        os.close(writer)
        writer = None
        with pytest.raises(EOFError):
            control.read(0.1)
    finally:
        os.close(reader)
        if writer is not None:
            os.close(writer)


def test_full_control_pipe_times_out_without_blocking_lifecycle():
    reader, writer = os.pipe()
    try:
        os.set_blocking(writer, False)
        while True:
            try:
                os.write(writer, b'x' * 65536)
            except BlockingIOError:
                break
        with pytest.raises(TimeoutError, match='write deadline'):
            worker.write_pipe(writer, b'{"stop": true}\n', 0.01)
    finally:
        os.close(reader)
        os.close(writer)


def natural_waits(monkeypatch, outcomes):
    calls, now = [], [0.0]
    outcomes = iter(outcomes)
    def waitpid(pid, flags):
        calls.append((pid, flags))
        outcome = next(outcomes, (0, 0))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(os, 'waitpid', waitpid)
    monkeypatch.setattr(worker.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(worker.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    return calls


def test_natural_descendant_drain_waits_for_zero_exit_and_echild(monkeypatch):
    calls = natural_waits(monkeypatch, [(0, 0), (4321, 0), ChildProcessError()])
    record = {}
    worker.drain_descendants(0.1, record)
    assert calls == [(-1, os.WNOHANG)] * 3
    assert record == {'descendants': [{'pid': 4321, 'returncode': 0}], 'echild': True}


def test_abnormal_descendant_exit_is_not_a_successful_drain(monkeypatch):
    natural_waits(monkeypatch, [(4321, 7 << 8), ChildProcessError()])
    record = {}
    with pytest.raises(RuntimeError, match='abnormally'):
        worker.drain_descendants(0.1, record)
    assert record['echild'] and record['descendants'][0]['returncode'] == 7


def test_live_descendant_timeout_does_not_kill_or_claim_echild(monkeypatch):
    natural_waits(monkeypatch, [(0, 0)])
    record = {}
    with pytest.raises(TimeoutError):
        worker.drain_descendants(0.03, record)
    assert not record.get('echild')


def execute_worker(monkeypatch, tmp_path, *, initial=(0, None), final=(0, -15),
                   sent=((), (15,)), commands=(), stop_error=False, stays_live=False):
    calls, forwarded = [], []
    processes = []
    for index, code in enumerate(initial):
        process = SimpleNamespace(pid=100 + index, returncode=code,
                                  stdin=SimpleNamespace(fileno=lambda index=index: 1000 + index))
        process.poll = lambda process=process: process.returncode
        processes.append(process)
    class OwnedGroup:
        def __init__(self, *args):
            self.processes = processes
            self.epoch_dir = tmp_path / 'group/epoch-0'
        def start(self):
            calls.append('start')
        def stop(self, reason):
            calls.append('stop')
            if not stays_live:
                for process, code in zip(processes, final):
                    process.returncode = code
            if stop_error:
                raise RuntimeError('injected cleanup failure')
            return {'peers': [{'signals': list(signals)} for signals in sent], 'cleanup': 'complete'}
    class Control:
        def __init__(self):
            self.commands = iter(commands)
        def read(self, timeout):
            message = next(self.commands, EOFError())
            if isinstance(message, Exception):
                raise message
            return message
    def drain(timeout, record):
        assert all(process.returncode is not None for process in processes)
        calls.append('drain')
        record['echild'] = True
    monkeypatch.setattr(worker, 'Group', OwnedGroup)
    monkeypatch.setattr(worker, 'process_start_time', lambda pid: str(pid * 10))
    monkeypatch.setattr(os, 'chdir', lambda path: calls.append(('chdir', path)))
    monkeypatch.setattr(worker, 'drain_descendants', drain)
    monkeypatch.setattr(worker, 'shm_inventory', lambda: {'cuda.shm.private': {'bytes': 128}})
    monkeypatch.setattr(worker, 'write_pipe', lambda fd, data, timeout: forwarded.append((fd, data)))
    monkeypatch.setattr(worker.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(os, 'environ', {})
    record = {}
    worker.run_epoch(dict(commands=[{'argv': ['unused']}], environment={}, cwd=str(tmp_path),
                          startup_timeout=1, stop_timeout=0.1), tmp_path, record, Control())
    assert calls[:2] == [('chdir', str(tmp_path)), 'start']
    return record, calls, forwarded


@pytest.mark.parametrize('sent,failed', [([15], False), ([], True), ([9], True)])
def test_worker_distinguishes_requested_termination_from_independent_peer_exit(
        monkeypatch, tmp_path, sent, failed):
    record, calls, _ = execute_worker(monkeypatch, tmp_path, sent=([], sent))
    assert record['peer_failed'] is failed
    assert record['retry_allowed'] is failed
    assert record['teardown_complete'] and calls[-1] == 'drain'


def test_kill_escalation_is_peer_failure_even_when_group_sent_the_signal(monkeypatch, tmp_path):
    record, _, _ = execute_worker(monkeypatch, tmp_path, final=(0, -9), sent=([], [15, 9]))
    assert record['peer_failed'] and record['retry_allowed']


@pytest.mark.parametrize('command', [{'stop': True}, EOFError()])
def test_owner_stop_or_eof_drains_without_restart(monkeypatch, tmp_path, command):
    record, calls, _ = execute_worker(
        monkeypatch, tmp_path, initial=(None, None), final=(-15, -15),
        sent=([15], [15]), commands=[command])
    assert record['requested_stop'] and record['teardown_complete']
    assert not record['retry_allowed'] and calls[-1] == 'drain'


def test_peer_command_then_stop_is_forwarded_before_drain(monkeypatch, tmp_path):
    record, calls, forwarded = execute_worker(
        monkeypatch, tmp_path, initial=(None, None), final=(-15, -15),
        sent=([15], [15]), commands=[{'peer': 1, 'stdin': 'finish'}, {'stop': True}])
    assert forwarded == [(1001, b'finish\n')]
    assert record['requested_stop'] and record['teardown_complete']
    assert calls[-1] == 'drain'


def test_cleanup_failure_still_drains_confirmed_dead_descendants(monkeypatch, tmp_path):
    record, calls, _ = execute_worker(monkeypatch, tmp_path, stop_error=True)
    assert calls[-1] == 'drain' and record['echild']
    assert 'error' in record and not record['teardown_complete'] and not record['retry_allowed']


def test_worker_never_reaps_descendants_while_direct_popen_peer_is_live(monkeypatch, tmp_path):
    record, calls, _ = execute_worker(monkeypatch, tmp_path, stop_error=True, stays_live=True)
    assert 'drain' not in calls and not record.get('echild')
    assert record['drain_error'] == 'direct peer death was not confirmed'
    assert not record['teardown_complete'] and not record['retry_allowed']
