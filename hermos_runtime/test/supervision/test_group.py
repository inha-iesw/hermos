"""Owned-process recovery without CUDA, kernel modules or real IPC objects."""

import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest


from hermos_runtime.supervision import group as recovery
from hermos_runtime.supervision import run as cli_module

HOST_POPEN = subprocess.Popen


@pytest.fixture
def child_file(tmp_path):
    path = tmp_path / 'peer.py'
    path.write_text('''import json, os, signal, sys, time
from pathlib import Path
mode = sys.argv[1]
if mode == 'ignore_term':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode == 'missing':
    time.sleep(30)
    raise SystemExit(1)
resources = []
if mode == 'resource':
    resources = [f'/dev/shm/rclpy_zc_gpu_v1_{os.getpid()}_0']
if mode == 'invalid':
    resources = ['/dev/shm/cuda.shm.unowned']
manifest = Path(os.environ['G3_RECOVERY_MANIFEST'])
temporary = manifest.with_suffix('.tmp')
temporary.write_text(json.dumps({'version': 1, 'pid': os.getpid(), 'resources': resources}))
temporary.replace(manifest)
ack = Path(os.environ['G3_RECOVERY_ACK'])
while not ack.exists():
    time.sleep(0.005)
print('ACK', flush=True)
if mode in ('zero', 'failure'):
    raise SystemExit(0 if mode == 'zero' else 3)
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    time.sleep(1)
raise SystemExit(1)
''')
    return path


@pytest.fixture(autouse=True)
def only_owned_python_children(monkeypatch, child_file):
    owned = []
    def permitted(args, **kwargs):
        assert isinstance(args, list) and args[:1] == [sys.executable]
        assert args[1] == str(child_file) and not kwargs.get('shell')
        child = HOST_POPEN(args, **kwargs)
        owned.append(child)
        return child

    def denied(*args, **kwargs):
        pytest.fail('unexpected subprocess in host recovery test')

    monkeypatch.setattr(subprocess, 'Popen', permitted)
    monkeypatch.setattr(subprocess, 'run', denied)
    monkeypatch.setattr(subprocess, 'check_output', denied)
    unlink = os.unlink
    def no_real_ipc_unlink(path, *args, **kwargs):
        if str(path).startswith(('/dev/shm/', '/tmp/cuda_memory_pool_')):
            pytest.fail('host recovery test attempted to unlink real IPC')
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(os, 'unlink', no_real_ipc_unlink)
    yield
    leaked = []
    for child in reversed(owned):
        if child.poll() is None:
            leaked.append(child.pid)
            child.kill()
            child.wait(timeout=2)
    assert not leaked, f'recovery left owned host children running: {leaked}'


def group(tmp_path, child_file, *modes, **kwargs):
    return recovery.Group([{'argv': [sys.executable, str(child_file), mode]} for mode in modes],
                          tmp_path / 'stage', startup_timeout=kwargs.pop('startup_timeout', 2),
                          stop_timeout=kwargs.pop('stop_timeout', 0.2), **kwargs)


def evidence(instance):
    return json.loads((instance.stage / 'recovery.json').read_text())['epochs']


@pytest.fixture
def ipc(monkeypatch):
    state = {'inode': 123, 'mode': stat.S_IFREG | 0o600, 'uid': os.getuid(), 'removed': [],
             'pins': {}, 'opened': [], 'closed': []}
    lstat = os.lstat
    unlink = os.unlink
    open_fd, fstat, close_fd = os.open, os.fstat, os.close

    def fake_lstat(path, *args, **kwargs):
        if str(path).startswith('/dev/shm/rclpy_zc_gpu_v1_'):
            return SimpleNamespace(st_dev=1, st_ino=state['inode'],
                                   st_mode=state['mode'], st_uid=state['uid'])
        return lstat(path, *args, **kwargs)

    def fake_unlink(path, *args, **kwargs):
        if str(path).startswith('/dev/shm/rclpy_zc_gpu_v1_'):
            assert all(p.poll() is not None for p in state['group'].processes)
            if len(state['removed']) == state.get('fail_after'):
                raise PermissionError('simulated unlink failure')
            state['removed'].append(str(path))
            return
        return unlink(path, *args, **kwargs)

    def fake_open(path, flags, *args, **kwargs):
        if str(path).startswith('/dev/shm/rclpy_zc_gpu_v1_'):
            assert flags == os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
            fd = open_fd('/dev/null', flags)
            state['pins'][fd] = fake_lstat(path)
            state['opened'].append(fd)
            if state.get('replace_on_open'):
                state['inode'] += 1
            return fd
        return open_fd(path, flags, *args, **kwargs)

    def fake_fstat(fd):
        return state['pins'][fd] if fd in state['pins'] else fstat(fd)

    def observed_close(fd):
        if fd in state['pins']:
            state['closed'].append(fd)
            del state['pins'][fd]
        return close_fd(fd)

    monkeypatch.setattr(os, 'lstat', fake_lstat)
    monkeypatch.setattr(os, 'unlink', fake_unlink)
    monkeypatch.setattr(os, 'open', fake_open)
    monkeypatch.setattr(os, 'fstat', fake_fstat)
    monkeypatch.setattr(os, 'close', observed_close)
    yield state
    leaked = list(state['pins'])
    for fd in leaked:
        close_fd(fd)
    assert not leaked and state['closed'] == state['opened']


def test_group_acknowledges_all_peers_and_restarts_the_entire_epoch(tmp_path, child_file):
    instance = group(tmp_path, child_file, 'healthy', 'healthy')
    try:
        instance.start()
        assert stat.S_IMODE(instance.stage.stat().st_mode) == 0o700
        assert stat.S_IMODE(instance.epoch_dir.stat().st_mode) == 0o700
        old = [p.pid for p in instance.processes]
        assert len(list(instance.epoch_dir.glob('*.ack'))) == 2
        instance.restart()
        assert not set(old) & {p.pid for p in instance.processes}
    finally:
        instance.stop()
    epochs = evidence(instance)
    assert len(epochs) == 2
    assert all(item['registered'] and item['confirmed_dead'] and
               item['cleanup'] == 'complete' for item in epochs)


def test_live_group_cannot_delete_registered_resources(tmp_path, child_file, ipc):
    instance = group(tmp_path, child_file, 'resource')
    ipc['group'] = instance
    try:
        instance.start()
        assert len(ipc['pins']) == 1
        with pytest.raises(recovery.RecoveryError, match='confirmed-dead'):
            instance._clean_resources()
        assert ipc['removed'] == []
    finally:
        instance.stop()
    assert len(ipc['removed']) == 1
    assert evidence(instance)[0]['removed_resources'] == ipc['removed']


def test_replaced_inode_is_preserved_and_forbids_restart(tmp_path, child_file, ipc):
    instance = group(tmp_path, child_file, 'resource')
    ipc['group'] = instance
    instance.start()
    assert len(ipc['pins']) == 1
    ipc['inode'] += 1
    with pytest.raises(recovery.RecoveryError, match='replaced'):
        instance.stop()
    assert all(p.poll() is not None for p in instance.processes)
    assert ipc['removed'] == []
    assert not ipc['pins'] and len(ipc['closed']) == 1
    with pytest.raises(recovery.RecoveryError, match='forbids restart'):
        instance.restart()
    assert evidence(instance)[0]['cleanup'] == 'refused'


def test_missing_registration_preserves_even_registered_inventory(tmp_path, child_file, ipc):
    instance = group(tmp_path, child_file, 'resource', 'missing', startup_timeout=0.3)
    ipc['group'] = instance
    with pytest.raises(recovery.RecoveryError, match='deadline'):
        instance.start()
    assert all(p.poll() is not None for p in instance.processes)
    assert ipc['removed'] == []
    assert not list(instance.epoch_dir.glob('*.ack'))
    with pytest.raises(recovery.RecoveryError, match='forbids restart'):
        instance.restart()


def test_resource_change_during_registration_returns_pin_without_cleanup(
        tmp_path, child_file, ipc):
    instance = group(tmp_path, child_file, 'resource')
    ipc.update(group=instance, replace_on_open=True)
    with pytest.raises(recovery.RecoveryError, match='changed while registering'):
        instance.start()
    assert all(p.poll() is not None for p in instance.processes)
    assert not ipc['removed'] and len(ipc['closed']) == 1
    assert not list(instance.epoch_dir.glob('*.ack'))


@pytest.mark.parametrize('mode', ['healthy', 'resource'])
def test_buffered_stdin_close_failure_still_cleans_and_forbids_restart(
        tmp_path, child_file, ipc, mode):
    instance = group(tmp_path, child_file, mode)
    ipc['group'] = instance
    instance.start()
    descriptors = [*instance._pidfds, *instance._pins]
    instance.processes[0].stdin.write('pending buffered input')
    with pytest.raises(recovery.RecoveryError, match='close'):
        instance.stop()
    assert all(p.poll() is not None for p in instance.processes)
    record = evidence(instance)[0]
    assert record['confirmed_dead'] and record['cleanup'] == 'complete'
    assert any('BrokenPipeError' in error for error in record['close_errors'])
    assert record['resource_pins_released'] == (1 if mode == 'resource' else 0)
    assert len(ipc['removed']) == (1 if mode == 'resource' else 0)
    assert not instance._pidfds and not instance._pins
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)
    with pytest.raises(recovery.RecoveryError, match='forbids restart'):
        instance.restart()


def test_partial_cleanup_failure_keeps_removed_inventory_and_forbids_restart(
        tmp_path, child_file, ipc):
    instance = group(tmp_path, child_file, 'resource', 'resource')
    ipc['group'] = instance
    instance.start()
    ipc['fail_after'] = 1
    with pytest.raises(recovery.RecoveryError, match='unlink failure'):
        instance.stop()
    assert len(ipc['removed']) == 1
    record = evidence(instance)[0]
    assert record['removed_resources'] == ipc['removed'] and record['cleanup'] == 'refused'
    with pytest.raises(recovery.RecoveryError):
        instance.restart()


@pytest.mark.parametrize('field,value', [('mode', stat.S_IFLNK | 0o777), ('uid', 4294967294)])
def test_unowned_or_symlink_resources_fail_closed(tmp_path, child_file, ipc, field, value):
    instance = group(tmp_path, child_file, 'resource')
    ipc['group'] = instance
    ipc[field] = value
    with pytest.raises(recovery.RecoveryError, match='type or owner'):
        instance.start()
    assert all(p.poll() is not None for p in instance.processes)
    assert ipc['removed'] == []


def test_foreign_namespace_registration_is_rejected(tmp_path, child_file, monkeypatch):
    original = os.stat
    def changed_namespace(path, *args, **kwargs):
        if str(path).startswith('/proc/') and '/self/' not in str(path) and '/ns/' in str(path):
            return SimpleNamespace(st_dev=-1, st_ino=-1)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, 'stat', changed_namespace)
    instance = group(tmp_path, child_file, 'healthy')
    with pytest.raises(recovery.RecoveryError, match='namespace'):
        instance.start()
    assert evidence(instance)[0]['confirmed_dead']


def test_unregistered_cuda_driver_name_is_never_cleaned(tmp_path, child_file):
    instance = group(tmp_path, child_file, 'invalid')
    with pytest.raises(recovery.RecoveryError, match='outside the owned IPC namespace'):
        instance.start()
    assert evidence(instance)[0]['confirmed_dead']


def test_term_deadline_escalates_only_owned_child_to_pidfd_kill(tmp_path, child_file):
    instance = group(tmp_path, child_file, 'ignore_term', stop_timeout=0.1)
    instance.start()
    instance.stop()
    entry = evidence(instance)[0]
    assert entry['peers'][0]['signals'] == [signal.SIGTERM, signal.SIGKILL]
    assert entry['returncodes'] == [-signal.SIGKILL]
    assert entry['cleanup'] == 'complete'


def test_pidfd_open_failure_kills_unreaped_child_and_prevents_restart(
        tmp_path, child_file, monkeypatch):
    def unavailable(pid):
        raise OSError('simulated pidfd exhaustion')
    monkeypatch.setattr(os, 'pidfd_open', unavailable)
    instance = group(tmp_path, child_file, 'healthy')
    with pytest.raises(recovery.RecoveryError, match='pidfd exhaustion'):
        instance.start()
    assert all(p.poll() is not None for p in instance.processes)
    entry = evidence(instance)[0]
    assert entry['peers'][0]['startup_kill_without_pidfd']
    with pytest.raises(recovery.RecoveryError):
        instance.restart()


def run_isolated_cli(monkeypatch, tmp_path, outcomes, *, requested_signal=None):
    cli = cli_module
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'commands': [{'argv': ['unused-peer']}]}))
    stage = tmp_path / 'cli-stage'
    epochs, handlers = [], {}

    class Epoch:
        def __init__(self, commands, path, startup, stop):
            self.stage = path
            self.stage.mkdir()
            self.calls = []
            self.result = outcomes[min(len(epochs), len(outcomes) - 1)]
            epochs.append(self)
        def start(self):
            self.calls.append('start')
            return self
        def poll(self):
            if requested_signal is not None:
                handlers[requested_signal](requested_signal, None)
                return None
            return 0
        def wait(self):
            self.calls.append('wait')
            return dict(self.result)
        def stop(self):
            self.calls.append('stop')
            return dict(self.result)

    monkeypatch.setattr(cli, 'IsolatedEpoch', Epoch)
    monkeypatch.setattr(cli.signal, 'signal', lambda number, handler: handlers.update({number: handler}))
    monkeypatch.setattr(sys, 'argv', ['hermos_supervisor', str(config), '--stage', str(stage),
                                     '--startup-timeout', '2', '--stop-timeout', '0.2'])
    code = cli.main()
    return code, json.loads((stage / 'cli.json').read_text()), epochs


@pytest.mark.parametrize('failed,expected,count', [(False, 0, 1), (True, 1, 2)])
def test_cli_normal_completion_and_bounded_failure_restarts(
        tmp_path, monkeypatch, failed, expected, count):
    outcome = dict(peer_failed=failed, retry_allowed=failed,
                   verified_teardown=True, requested_stop=False)
    code, summary, epochs = run_isolated_cli(monkeypatch, tmp_path, [outcome])
    assert code == summary['exit_code'] == expected
    assert summary['restarts'] == count - 1
    assert len(epochs) == count
    assert [epoch.stage.name for epoch in epochs] == [f'epoch-{i}' for i in range(count)]
    assert all(epoch.calls == ['start', 'wait'] for epoch in epochs)


def test_completed_epoch_does_not_start_another_namespace(tmp_path, monkeypatch):
    outcome = dict(peer_failed=False, retry_allowed=False,
                   verified_teardown=True, requested_stop=False)
    code, summary, epochs = run_isolated_cli(monkeypatch, tmp_path, [outcome])
    assert code == 0 and summary['restarts'] == 0
    assert len(epochs) == 1 and epochs[0].calls == ['start', 'wait']


def test_cli_signal_stops_its_owned_epoch_and_records_exit(tmp_path, monkeypatch):
    outcome = dict(peer_failed=False, retry_allowed=False,
                   verified_teardown=True, requested_stop=True)
    code, summary, epochs = run_isolated_cli(
        monkeypatch, tmp_path, [outcome], requested_signal=signal.SIGTERM)
    assert code == summary['exit_code'] == 128 + signal.SIGTERM
    assert len(epochs) == 1 and epochs[0].calls == ['start', 'stop']


@pytest.mark.parametrize('changed', [
    {'verified_teardown': False}, {'retry_allowed': False}, {'requested_stop': True},
])
def test_cli_refuses_unverified_or_forbidden_restart(tmp_path, monkeypatch, changed):
    outcome = dict(peer_failed=True, retry_allowed=True,
                   verified_teardown=True, requested_stop=False)
    outcome.update(changed)
    code, summary, epochs = run_isolated_cli(monkeypatch, tmp_path, [outcome])
    assert code == 1 and summary['restarts'] == 0
    assert len(epochs) == 1 and 'error' in summary


@pytest.mark.parametrize('path', [
    '/dev/shm/rclpy_zc_gpu_v1_999_0', '/tmp/../dev/shm/rclpy_zc_gpu_v1_123_0',
    '/dev/shm/cuda.shm.123', '/tmp/cuda_memory_pool_not-a-uuid.sock',
])
def test_unowned_resource_names_are_rejected_before_lstat(path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('invalid path reached lstat')
    monkeypatch.setattr(os, 'lstat', forbidden)
    with pytest.raises(recovery.RecoveryError):
        recovery._resource(path, 123)
