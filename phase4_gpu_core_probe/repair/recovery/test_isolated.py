"""Host-only owner tests; sudo, namespace operations and real processes are blocked."""

import ctypes
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
from types import SimpleNamespace

import pytest

import isolated


@pytest.fixture(autouse=True)
def block_privileged_or_unowned_operations(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail('unmocked privileged operation or process operation')
    monkeypatch.setattr(ctypes, 'CDLL', blocked)
    for name in ('Popen', 'run', 'check_output'):
        monkeypatch.setattr(subprocess, name, blocked)
    for name in ('setgroups', 'setresgid', 'setresuid', 'setgid', 'setuid',
                 'setegid', 'seteuid', 'waitpid', 'pidfd_open', 'kill', 'fork'):
        monkeypatch.setattr(os, name, blocked)
    monkeypatch.setattr(signal, 'pidfd_send_signal', blocked)


def simulated_epoch(monkeypatch, tmp_path):
    pid, pidfd = 9020, 990001
    state = dict(messages=[], opened=[], closed=[], selects=[], wrapper_status=None,
                 wrapper_code=0, worker_dead=False, dies_on_wait=True, dies_on_eof=False,
                 start_time='1234', uids=[1000] * 4, wait_calls=0, close_error=False,
                 stop_write_timeout=False, identity_file='valid')
    state['result'] = dict(version=1, worker_pid=pid, teardown_complete=True, echild=True,
                           peer_failed=True, retry_allowed=True, requested_stop=False)

    class Input:
        closed = False
        def fileno(self):
            return 990002
        def close(self):
            was_closed, self.closed = self.closed, True
            if state['close_error'] and not was_closed:
                raise BrokenPipeError('injected control close failure')

    class Wrapper:
        pid = 9010
        stdin = Input()
        def poll(self):
            return state['wrapper_status']
        def wait(self, timeout):
            assert timeout is None or timeout > 0
            state['wait_calls'] += 1
            if state.get('wait_timeout'):
                raise subprocess.TimeoutExpired('wrapper', timeout)
            state['wrapper_status'] = state['wrapper_code']
            state['worker_dead'] = state['dies_on_wait']
            result = state['result']
            if result is not None:
                (epoch.stage / 'worker-result.json').write_text(
                    result if isinstance(result, str) else json.dumps(result))
            return state['wrapper_code']

    wrapper = Wrapper()
    def launch(command, **kwargs):
        assert command[:3] == ['sudo', '-n', '/usr/bin/python3']
        assert '-B' in command and '-I' in command and command[5] == str(isolated.BOOTSTRAP)
        assert command[-2].endswith('/namespace_worker.py') and command[-1] == str(epoch.stage)
        assert 'LD_PRELOAD' not in kwargs['env'] and not kwargs.get('shell')
        if state['identity_file'] != 'missing':
            identity = ('{' if state['identity_file'] == 'corrupt' else json.dumps(
                {'pid': pid, 'start_time': state['start_time']}))
            (epoch.stage / 'worker.json').write_text(identity)
        (epoch.stage / 'ready.json').write_text(json.dumps(
            {'peers': [9030, 9031], 'epoch_dir': str(epoch.stage / 'group/epoch-0')}))
        return wrapper

    def write(fd, data, timeout):
        assert fd == 990002 and timeout > 0
        value = json.loads(data)
        state['messages'].append(value)
        if state['stop_write_timeout'] and value == {'stop': True}:
            raise TimeoutError('injected stop pipe timeout')

    def open_pidfd(value):
        state['opened'].append(value)
        assert value == pid  # The sudo wrapper is a different process.
        return pidfd
    def close(fd):
        assert fd == pidfd
        state['closed'].append(fd)
    def readiness(read, write, error, timeout):
        assert read == [pidfd] and not write and not error
        state['selects'].append(timeout)
        if timeout:
            assert wrapper.stdin.closed
            state['worker_dead'] = state['dies_on_eof']
        return (read if state['worker_dead'] else [], [], [])

    read_text = Path.read_text
    def contents(path, *args, **kwargs):
        if str(path) == f'/proc/{pid}/stat':
            return f'{pid} (namespace worker) ' + ' '.join(['0'] * 19 + ['1234'])
        if str(path) == f'/proc/{pid}/status':
            return 'Uid:\t' + '\t'.join(map(str, state['uids'])) + '\n'
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(os, 'getuid', lambda: 1000)
    monkeypatch.setattr(os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(os, 'getgroups', lambda: [1000, 44])
    monkeypatch.setattr(os, 'environ', {'INHERITED': 'preserved', **isolated.REQUIRED_ENVIRONMENT})
    monkeypatch.setattr(isolated, 'trusted_bootstrap', lambda: str(isolated.BOOTSTRAP))
    monkeypatch.setattr(os, 'statvfs', lambda path: SimpleNamespace(f_blocks=2, f_frsize=4096))
    monkeypatch.setattr(Path, 'read_text', contents)
    monkeypatch.setattr(subprocess, 'Popen', launch)
    monkeypatch.setattr(isolated, 'write_pipe', write)
    monkeypatch.setattr(os, 'pidfd_open', open_pidfd)
    monkeypatch.setattr(os, 'close', close)
    monkeypatch.setattr(isolated.select, 'select', readiness)
    epoch = isolated.IsolatedEpoch(
        [{'argv': ['unused-peer'], 'env': {'LD_PRELOAD': '/peer-only/hook'}}],
        tmp_path / 'epoch', startup_timeout=0.5, stop_timeout=0.1)
    return epoch, state, wrapper


def test_owner_pins_actual_worker_before_ack_and_preserves_peer_only_preload(monkeypatch, tmp_path):
    epoch, state, wrapper = simulated_epoch(monkeypatch, tmp_path)
    assert epoch.start() is epoch
    assert state['opened'] == [9020] and wrapper.pid != state['opened'][0]
    assert state['messages'][0]['commands'][0]['env']['LD_PRELOAD'] == '/peer-only/hook'
    assert state['messages'][0]['environment'] == {'INHERITED': 'preserved', **isolated.REQUIRED_ENVIRONMENT}
    assert 'groups' not in state['messages'][0] and 'shm_bytes' not in state['messages'][0]
    assert state['messages'][1] == {'ack': True}
    assert epoch.peers == [9030, 9031]
    epoch.send(1, 'finish')
    assert state['messages'][-1] == {'peer': 1, 'stdin': 'finish'}
    result = epoch.stop()
    assert result['verified_teardown'] and state['messages'][-1] == {'stop': True}
    assert state['closed'] == [990001] and wrapper.stdin.closed


@pytest.mark.parametrize('field,value,match', [
    ('start_time', 'reused', 'PID was reused'), ('uids', [0] * 4, 'did not drop'),
])
def test_invalid_worker_identity_never_receives_ack(monkeypatch, tmp_path, field, value, match):
    epoch, state, _ = simulated_epoch(monkeypatch, tmp_path)
    state[field] = value
    with pytest.raises(isolated.RecoveryError, match=match):
        epoch.start()
    assert {'ack': True} not in state['messages']
    assert state['closed'] == [990001]
    assert epoch.result is None or (
        not epoch.result.get('verified_teardown') and not epoch.result.get('retry_allowed'))


@pytest.mark.parametrize('identity_file', ['missing', 'corrupt'])
def test_missing_or_corrupt_identity_never_pins_or_acknowledges_worker(
        monkeypatch, tmp_path, identity_file):
    epoch, state, wrapper = simulated_epoch(monkeypatch, tmp_path)
    state['identity_file'] = identity_file
    if identity_file == 'missing':
        state.update(wrapper_status=70, wrapper_code=70)
    with pytest.raises((isolated.RecoveryError, json.JSONDecodeError)):
        epoch.start()
    assert not state['opened'] and {'ack': True} not in state['messages']
    assert wrapper.stdin.closed and not epoch._streams
    assert epoch.result is None or not epoch.result.get('verified_teardown')


@pytest.mark.parametrize('problem', ['missing', 'corrupt', 'wrong_pid', 'not_echild', 'not_complete'])
def test_bad_worker_evidence_never_authorizes_retry(monkeypatch, tmp_path, problem):
    epoch, state, wrapper = simulated_epoch(monkeypatch, tmp_path)
    epoch.start()
    if problem == 'missing':
        state['result'] = None
    elif problem == 'corrupt':
        state['result'] = '{'
    else:
        key, value = {'wrong_pid': ('worker_pid', 9010), 'not_echild': ('echild', False),
                      'not_complete': ('teardown_complete', False)}[problem]
        state['result'][key] = value
    with pytest.raises((isolated.RecoveryError, FileNotFoundError, json.JSONDecodeError)):
        epoch.wait()
    assert epoch.result is None or not epoch.result.get('verified_teardown')
    assert wrapper.stdin.closed and state['closed'] == [990001]
    with pytest.raises(isolated.RecoveryError):
        epoch.wait()


def test_dead_wrapper_is_not_proof_that_namespace_worker_died(monkeypatch, tmp_path):
    epoch, state, wrapper = simulated_epoch(monkeypatch, tmp_path)
    epoch.start()
    state.update(dies_on_wait=False, dies_on_eof=False)
    with pytest.raises(isolated.RecoveryError, match='worker death was not confirmed'):
        epoch.wait()
    assert wrapper.stdin.closed and any(timeout > 0 for timeout in state['selects'])
    assert epoch.result is None and not state['closed']
    state['dies_on_wait'] = True
    epoch.stop()


def test_wrapper_exit_closes_input_and_waits_for_worker_natural_drain(monkeypatch, tmp_path):
    epoch, state, wrapper = simulated_epoch(monkeypatch, tmp_path)
    epoch.start()
    state.update(dies_on_wait=False, dies_on_eof=True)
    assert epoch.wait()['verified_teardown']
    assert wrapper.stdin.closed and any(timeout > 0 for timeout in state['selects'])


def test_nonzero_wrapper_exit_rejects_even_complete_worker_evidence(monkeypatch, tmp_path):
    epoch, state, _ = simulated_epoch(monkeypatch, tmp_path)
    epoch.start()
    state['wrapper_code'] = 70
    with pytest.raises(isolated.RecoveryError):
        epoch.wait()
    assert not epoch.result.get('verified_teardown')


def test_control_close_error_releases_pidfd_and_denies_retry(monkeypatch, tmp_path):
    epoch, state, _ = simulated_epoch(monkeypatch, tmp_path)
    epoch.start()
    state['close_error'] = True
    with pytest.raises(isolated.RecoveryError, match='handle close failed'):
        epoch.wait()
    assert not epoch.result['verified_teardown'] and state['closed'] == [990001]


def test_stop_pipe_timeout_still_closes_input_and_waits_for_teardown(monkeypatch, tmp_path):
    epoch, state, wrapper = simulated_epoch(monkeypatch, tmp_path)
    epoch.start()
    state['stop_write_timeout'] = True
    with pytest.raises(isolated.RecoveryError, match='stop control failed after cleanup'):
        epoch.stop()
    result = epoch.result
    assert result['verified_teardown'] and wrapper.stdin.closed
    assert not result['retry_allowed'] and (epoch.stage / 'control-errors.json').exists()
    assert state['wait_calls'] == 1 and state['closed'] == [990001]


def test_failed_wait_preserves_first_worker_death_evidence(monkeypatch, tmp_path):
    epoch, state, _ = simulated_epoch(monkeypatch, tmp_path)
    epoch.start()
    state['result'] = None
    with pytest.raises(Exception):
        epoch.wait()
    first = (epoch.stage / 'owner.json').read_bytes()
    assert json.loads(first)['worker_death_confirmed']
    with pytest.raises(Exception):
        epoch.stop()
    assert (epoch.stage / 'owner.json').read_bytes() == first
    assert state['wait_calls'] == 1


def test_wait_timeout_preserves_handles_for_later_stop(monkeypatch, tmp_path):
    epoch, state, wrapper = simulated_epoch(monkeypatch, tmp_path)
    epoch.start()
    state['wait_timeout'] = True
    with pytest.raises(subprocess.TimeoutExpired):
        epoch.wait(timeout=0.01)
    assert not wrapper.stdin.closed and not state['closed']
    assert epoch.worker_pidfd == 990001
    state['wait_timeout'] = False
    assert epoch.stop()['verified_teardown']
    assert state['closed'] == [990001]


@pytest.mark.parametrize('component,uid,mode', [
    (0, 1000, stat.S_IFREG | 0o644), (0, 0, stat.S_IFREG | 0o664),
    (0, 0, stat.S_IFLNK | 0o777), (1, 1000, stat.S_IFDIR | 0o755),
    (1, 0, stat.S_IFDIR | 0o775), (2, 0, stat.S_IFDIR | 0o777),
])
def test_bootstrap_rejects_caller_writable_or_symlinked_installation(
        monkeypatch, component, uid, mode):
    paths = (isolated.BOOTSTRAP, *isolated.BOOTSTRAP.parents)
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        if path not in paths:
            return original(path, *args, **kwargs)
        if path == paths[component]:
            return SimpleNamespace(st_uid=uid, st_mode=mode)
        return SimpleNamespace(st_uid=0, st_mode=(stat.S_IFREG | 0o644) if path == paths[0]
                               else (stat.S_IFDIR | 0o755))
    monkeypatch.setattr(Path, 'lstat', lstat)
    with pytest.raises(isolated.RecoveryError, match='root-owned'):
        isolated.trusted_bootstrap()


def test_bootstrap_rejects_installed_source_drift(monkeypatch):
    monkeypatch.setattr(Path, 'lstat', lambda path: SimpleNamespace(
        st_uid=0, st_mode=(stat.S_IFREG | 0o644) if path == isolated.BOOTSTRAP
        else (stat.S_IFDIR | 0o755)))
    monkeypatch.setattr(Path, 'read_bytes', lambda path:
                        b'old installed helper' if path == isolated.BOOTSTRAP else b'current helper')
    with pytest.raises(isolated.RecoveryError, match='differs'):
        isolated.trusted_bootstrap()


def test_wrapper_loss_with_eof_close_error_still_confirms_worker_death(monkeypatch, tmp_path):
    epoch, state, _ = simulated_epoch(monkeypatch, tmp_path)
    epoch.start()
    state.update(dies_on_wait=False, dies_on_eof=True, close_error=True)
    with pytest.raises(isolated.RecoveryError):
        epoch.wait()
    owner = json.loads((epoch.stage / 'owner.json').read_text())
    assert owner.get('worker_death_confirmed')
    assert not owner['verified_teardown']
    assert any(timeout > 0 for timeout in state['selects'])
    assert state['closed'] == [990001]
    assert not epoch.result.get('retry_allowed')


@pytest.mark.parametrize('override', [
    {}, {'HERMOS_BRIDGE_MODE': 'on', 'HERMOS_NO_DISCOVERY_AGENT': '1'},
    {'HERMOS_BRIDGE_MODE': 'off', 'HERMOS_NO_DISCOVERY_AGENT': '0'},
])
def test_unsupported_peer_environment_is_rejected_before_stage_creation(monkeypatch, tmp_path, override):
    monkeypatch.setattr(os, 'getuid', lambda: 1000)
    monkeypatch.setattr(os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(os, 'environ', {})
    stage = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='BRIDGE_MODE'):
        isolated.IsolatedEpoch([{'argv': ['unused'], 'env': override}], stage)
    assert not stage.exists()
