"""Verify fresh GPU namespaces, complete descendant drain and healthy isolation."""

import ctypes
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time

import pytest

from isolated import IsolatedEpoch
from test_device import Pair


pytestmark = pytest.mark.skipif(
    os.environ.get('G3_RUN_TRANSPORT') != '1', reason='requires verified device session')
EVENT_PROBE = Path(__file__).resolve().parents[1] / 'residency/event_probe.py'
PEER_ENV = {'HERMOS_BRIDGE_MODE': 'off', 'HERMOS_NO_DISCOVERY_AGENT': '1'}


def host_shm():
    inventory = {}
    for path in Path('/dev/shm').iterdir():
        info = path.lstat()
        inventory[path.name] = (info.st_dev, info.st_ino, info.st_size)
    return inventory


def assert_isolation(stage, other=None, *, require_live=False):
    worker = json.loads((stage / 'worker.json').read_text())
    isolation = worker['isolation']
    host = {name: os.readlink('/proc/self/ns/' + name) for name in ('pid', 'ipc', 'mnt')}
    assert isolation['before'] == host
    assert isolation['initial_shm'] == {}
    assert isolation['after']['pid'] == host['pid']
    assert isolation['shm_device'] != os.stat('/dev/shm').st_dev
    for name in ('ipc', 'mnt'):
        assert isolation['after'][name] != host[name]
    if require_live:
        # Independent /proc observations, not just the worker's self-report.
        proc = Path('/proc') / str(worker['pid'])
        observed = {name: os.readlink(proc / 'ns' / name) for name in host}
        assert observed == isolation['after']
        status = dict(line.split(':', 1) for line in (proc / 'status').read_text().splitlines()
                      if ':' in line)
        assert [int(value) for value in status['Uid'].split()] == [os.getuid()] * 4
        assert all(int(status[name], 16) == 0 for name in ('CapInh', 'CapPrm', 'CapEff', 'CapAmb'))
        assert int(status['NoNewPrivs']) == 1
        device = (proc / 'root/dev/shm').stat().st_dev
        assert device == isolation['shm_device']
        (stage / 'test-observed-isolation.json').write_text(json.dumps({
            'worker_pid': worker['pid'], 'namespaces': observed, 'shm_device': device,
            'uid': [int(value) for value in status['Uid'].split()],
            'capabilities': 0, 'no_new_privs': True}, indent=2) + '\n')
    if other is not None:
        concurrent = json.loads((other / 'worker.json').read_text())['isolation']
        assert isolation['shm_device'] != concurrent['shm_device']
        for name in ('ipc', 'mnt'):
            assert isolation['after'][name] != concurrent['after'][name]
    # Released namespace IDs may be reused. Only concurrently live epochs are compared.
    return isolation


def assert_teardown(record, *, failed, daemons):
    assert record['verified_teardown'] and record['teardown_complete'] and record['echild']
    assert record['peer_failed'] is failed
    assert record['retry_allowed'] is failed
    assert record['cleanup_complete']
    assert record['isolation']['initial_shm'] == {}
    if daemons:
        assert 1 <= len(record['descendants']) <= len(record['returncodes'])
    else:
        assert record['descendants'] == []
    assert all(item['returncode'] == 0 for item in record['descendants'])
    remaining = dict(record['final_shm'])
    registry = remaining.pop('hermos_type_registry', None)
    if daemons:
        # TypeRegistryWriter removes per-PID files, leaving its registry and
        # one IPC-namespace directory. This private tmpfs disappears with the epoch.
        assert registry is not None and 0 < registry['bytes'] <= 60
    else:
        assert registry is None
    assert len(remaining) <= len(record['returncodes'])
    assert all(name.startswith('cuda.shm.') for name in remaining)
    assert all(item['bytes'] == 81920 and item['inode'] > 0 for item in remaining.values())


@pytest.fixture
def epochs(tmp_path):
    root = Path(os.environ.get('G3_PEER_LOG_ROOT', tmp_path))
    root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='namespace-recovery-', dir=root)).resolve()
    baseline, active = host_shm(), []

    def start(commands):
        epoch = IsolatedEpoch(commands, stage / f'epoch-{len(active)}',
                              startup_timeout=10, stop_timeout=5)
        active.append(epoch)
        epoch.start()
        assert_isolation(epoch.stage)
        assert host_shm() == baseline
        return epoch

    try:
        yield start, baseline
    finally:
        errors = []
        for epoch in reversed(active):
            try:
                record = epoch.stop()
                assert record['verified_teardown'] and record['echild']
                assert all(item['returncode'] == 0 for item in record['descendants'])
            except Exception as error:
                errors.append(f'{epoch.stage}: {error!r}')
        assert not errors, errors
        assert host_shm() == baseline


class NamespacePair(Pair):
    """Reuse the native protocol oracle with a namespace worker as sole wait owner."""

    def __init__(self, start, fault=None, blocks=1):
        hook = Path(os.environ.get('RCLPY_HERMOS_DEVICE_BUNDLE',
            Path(__file__).resolve().parents[3] / 'build/hermos/device-arm64')) / 'lib'
        env = dict(HERMOS_DEVICE_PATH='/dev/hermos_rclpy', HERMOS_BRIDGE_MODE='off',
                   HERMOS_NO_DISCOVERY_AGENT='1',
                   LD_PRELOAD=str(hook / 'libhermos_heaphook.so'),
                   LD_LIBRARY_PATH=str(hook) + ':' + os.environ.get('LD_LIBRARY_PATH', ''))
        topic = f'/g3_namespace_{os.getpid()}_{time.monotonic_ns()}'
        commands = []
        for role, limit in (('publisher', blocks), ('subscriber', 1)):
            peer_env = env.copy()
            binary = os.environ['G3_TRANSPORT_PEER']
            if role == 'subscriber' and fault is not None:
                binary = os.environ['G3_CLEANUP_PEER']
                peer_env['G3_IMPORT_FAILURE'] = fault
            commands.append({'argv': [binary, role, topic, str(limit)], 'env': peer_env})
        self.group = start(commands)
        self.ready()

    def ask(self, peer, command):
        self.group.send(peer, command)
        return self.receive(peer)


def registered_event_command(marker=None):
    registration = '''import json, os, runpy, sys, time
from pathlib import Path
probe = sys.argv[1]
marker = Path(sys.argv[2]) if len(sys.argv) > 2 else None
manifest = Path(os.environ['G3_RECOVERY_MANIFEST'])
temporary = manifest.with_suffix('.tmp')
temporary.write_text(json.dumps({'version': 1, 'pid': os.getpid(), 'resources': []}))
temporary.replace(manifest)
ack = Path(os.environ['G3_RECOVERY_ACK'])
deadline = time.monotonic() + float(os.environ['G3_RECOVERY_START_TIMEOUT'])
while not ack.exists():
    if time.monotonic() >= deadline:
        raise TimeoutError('registration ACK deadline expired')
    time.sleep(0.005)
sys.argv = [probe, '--child', 'runtime_event']
runpy.run_path(sys.argv[0], run_name='__main__')
if marker is not None:
    try:
        marker.touch(exist_ok=False)
    except FileExistsError:
        pass
    else:
        raise SystemExit(1)
'''
    command = [sys.executable, '-u', '-c', registration, str(EVENT_PROBE)]
    if marker is not None:
        command.append(str(marker))
    return {'argv': command, 'env': PEER_ENV.copy()}


def assert_runtime_event(record, log, pid):
    result = json.loads(log.read_text())
    assert result['pid'] == pid and result['mode'] == 'runtime_event'
    assert result['calls'] == [[name, 0] for name in (
        'cudaSetDevice', 'cudaEventCreateWithFlags', 'cudaEventRecord',
        'cudaEventSynchronize', 'cudaIpcGetEventHandle', 'cudaEventDestroy')]
    assert len(record['final_shm']) == 1
    residue = next(iter(record['final_shm'].values()))
    assert residue['bytes'] == 81920 and residue['inode'] > 0


def test_runtime_ipc_event_residue_is_confined_to_each_fresh_namespace(epochs):
    start, baseline = epochs
    for _ in range(3):
        epoch = start([registered_event_command()])
        record = epoch.wait()
        assert_teardown(record, failed=False, daemons=False)
        assert record['returncodes'] == [0]
        assert_runtime_event(record, epoch.epoch_dir / 'peer-0.stdout', epoch.peers[0])
        assert host_shm() == baseline


def test_reader_crashes_replace_complete_namespaces_and_preserve_healthy_pair(epochs):
    start, baseline = epochs
    healthy, crashed = NamespacePair(start), NamespacePair(start)
    healthy_pids = list(healthy.group.peers)
    for cycle in range(3):
        assert_isolation(crashed.group.stage, healthy.group.stage)
        packet = crashed.publish(31 + cycle)
        assert crashed.ask(1, 'take')[:2] == ['MAPPED', packet[1]]
        assert crashed.ask(0, 'probe') == ['FULL']
        # Pin only for fault injection. The namespace worker remains the sole peer reaper.
        pidfd = os.pidfd_open(crashed.group.peers[1])
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        finally:
            os.close(pidfd)
        healthy.checksum(71 + cycle)
        record = crashed.group.wait()
        assert_teardown(record, failed=True, daemons=True)
        assert record['returncodes'][1] == -signal.SIGKILL
        assert host_shm() == baseline
        crashed = NamespacePair(start)
        assert_isolation(crashed.group.stage, healthy.group.stage)
        crashed.checksum(41 + cycle)
        assert healthy.group.peers == healthy_pids and healthy.group.poll() is None
    assert_teardown(crashed.group.stop(), failed=False, daemons=True)
    assert_teardown(healthy.group.stop(), failed=False, daemons=True)


@pytest.mark.parametrize('fault', ['partial_unmap', 'event_destroy'])
def test_terminal_poison_restarts_in_fresh_namespace_after_complete_drain(epochs, fault):
    start, baseline = epochs
    healthy, poisoned = NamespacePair(start), NamespacePair(start, fault)
    healthy_pids = list(healthy.group.peers)
    assert_isolation(poisoned.group.stage, healthy.group.stage)
    packet = poisoned.publish(53)
    assert poisoned.ask(1, 'stash') == ['STASHED', packet[1]]
    mapped = poisoned.ask(1, 'map_only')
    if fault == 'partial_unmap':
        assert mapped == ['POISONED']
    else:
        assert mapped[:2] == ['MAPPED', packet[1]]
        assert poisoned.ask(1, 'drop') == ['DROPPED']
    deadline = time.monotonic() + 3
    while True:
        stats = poisoned.ask(1, 'fault_stats')
        if stats[-1] == '1':
            break
        assert time.monotonic() < deadline
        time.sleep(0.005)
    assert len(stats) == 8 and stats[1:4] == ['1', '1', '1']
    assert int(stats[4]) >= 1
    assert poisoned.ask(1, 'close_read') == ['READ_CLOSED', '0']
    assert poisoned.ask(0, 'probe') == ['FULL']
    healthy.checksum(79)
    poisoned.group.send(1, 'quit')
    record = poisoned.group.wait()
    assert_teardown(record, failed=True, daemons=True)
    assert record['returncodes'][1] == 1
    assert 'GPU subscriber did not drain; peer restart required' in (
        poisoned.group.epoch_dir / 'peer-1.stderr').read_text()
    assert host_shm() == baseline
    healthy.checksum(83)
    fresh = NamespacePair(start)
    assert_isolation(fresh.group.stage, healthy.group.stage)
    fresh.checksum(59)
    healthy.checksum(89)
    assert healthy.group.peers == healthy_pids and healthy.group.poll() is None
    assert_teardown(fresh.group.stop(), failed=False, daemons=True)
    assert_teardown(healthy.group.stop(), failed=False, daemons=True)


def test_cli_restarts_failed_runtime_peer_once_in_a_fresh_namespace(epochs, tmp_path):
    _, baseline = epochs
    root = Path(os.environ.get('G3_PEER_LOG_ROOT', tmp_path))
    session = Path(tempfile.mkdtemp(prefix='namespace-cli-', dir=root)).resolve()
    assert not session.is_relative_to('/dev/shm')
    marker = session / 'first-failure.marker'
    config = session / 'commands.json'
    config.write_text(json.dumps({'commands': [registered_event_command(marker)]}))
    stage = session / 'run'
    with (session / 'cli.stdout').open('w') as stdout, (session / 'cli.stderr').open('w') as stderr:
        process = subprocess.run(
            [sys.executable, str(Path(__file__).with_name('run.py')), str(config),
             '--stage', str(stage), '--max-restarts', '1'],
            stdout=stdout, stderr=stderr, timeout=60)
    assert process.returncode == 0, session
    summary = json.loads((stage / 'cli.json').read_text())
    assert summary['exit_code'] == 0 and summary['restarts'] == 1
    assert marker.is_file()
    assert sorted(path.name for path in stage.glob('epoch-*')) == ['epoch-0', 'epoch-1']
    for index, failed in enumerate((True, False)):
        epoch = stage / f'epoch-{index}'
        assert_isolation(epoch)
        record = json.loads((epoch / 'worker-result.json').read_text())
        owner = json.loads((epoch / 'owner.json').read_text())
        worker = json.loads((epoch / 'worker.json').read_text())
        ready = json.loads((epoch / 'ready.json').read_text())
        assert owner['worker_pid'] == worker['pid'] == record['worker_pid']
        assert owner['wrapper_exit_code'] == 0 and owner['worker_death_confirmed']
        assert owner['verified_teardown']
        record['verified_teardown'] = owner['verified_teardown']
        assert_teardown(record, failed=failed, daemons=False)
        assert record['returncodes'] == [1 if failed else 0] and not record['requested_stop']
        assert_runtime_event(record, Path(ready['epoch_dir']) / 'peer-0.stdout', ready['peers'][0])
    assert host_shm() == baseline


def test_crashed_unpublished_pool_inventory_is_cleaned_inside_private_namespace(epochs):
    start, baseline = epochs
    pair = NamespacePair(start, blocks=3)
    assert_isolation(pair.group.stage, require_live=True)
    worker = json.loads((pair.group.stage / 'worker.json').read_text())['pid']
    manifest = json.loads((pair.group.epoch_dir / 'peer-0.json').read_text())
    paths = [Path(path) for path in manifest['resources']]
    assert len(paths) == 6
    assert all((Path(f'/proc/{worker}/root') / path.relative_to('/')).exists() for path in paths)
    assert json.loads((pair.group.epoch_dir / 'peer-1.json').read_text())['resources'] == []
    # Normal shutdown can unlink the inventory before Group needs to act. Kill
    # the owned publisher to require supervisor cleanup of all unpublished names.
    pidfd = os.pidfd_open(pair.group.peers[0])
    try:
        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
    finally:
        os.close(pidfd)
    record = pair.group.wait()
    assert_teardown(record, failed=True, daemons=True)
    assert record['returncodes'][0] == -signal.SIGKILL
    group = json.loads((pair.group.stage / 'group/recovery.json').read_text())['epochs'][0]
    assert group['registered'] and group['confirmed_dead'] and group['cleanup'] == 'complete'
    assert group['resource_pins'] == group['resource_pins_released'] == 6
    assert set(group['removed_resources']) == set(manifest['resources'])
    assert all(path.name not in record['final_shm'] for path in paths if path.parent == Path('/dev/shm'))
    assert all(not path.exists() for path in paths if path.parent == Path('/tmp'))
    owner = json.loads((pair.group.stage / 'owner.json').read_text())
    assert owner['worker_death_confirmed'] and owner['verified_teardown']
    assert host_shm() == baseline


def registered_control_command(mode, trigger):
    """Non-GPU peer for real sudo/namespace failure paths, with no unlink daemon."""
    code = '''import ctypes, json, os, signal, sys, time
from pathlib import Path
mode, trigger = sys.argv[1], Path(sys.argv[2])
if mode == 'ignore_term':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
manifest = Path(os.environ['G3_RECOVERY_MANIFEST'])
temporary = manifest.with_suffix('.tmp')
temporary.write_text(json.dumps({'version': 1, 'pid': os.getpid(), 'resources': []}))
temporary.replace(manifest)
ack = Path(os.environ['G3_RECOVERY_ACK'])
deadline = time.monotonic() + float(os.environ['G3_RECOVERY_START_TIMEOUT'])
while not ack.exists():
    if time.monotonic() >= deadline:
        raise TimeoutError('registration ACK deadline expired')
    time.sleep(0.005)
if mode == 'descendant':
    child = os.fork()
    if child == 0:
        # This non-GPU child exits naturally; no test sends it a signal.
        death_signal = ctypes.c_int()
        assert ctypes.CDLL(None).prctl(2, ctypes.byref(death_signal), 0, 0, 0) == 0
        assert death_signal.value == 0  # Linux clears PDEATHSIG across fork.
        manifest.with_suffix('.descendant-state').write_text(json.dumps({
            'pid': os.getpid(), 'parent_death_signal': death_signal.value}))
        deadline = time.monotonic() + 40
        while not trigger.with_suffix('.child').exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        os._exit(0)
    manifest.with_suffix('.descendant').write_text(str(child))
manifest.with_suffix('.running').touch()
while mode not in ('exit_when_released', 'descendant') or not trigger.exists():
    time.sleep(0.005)
'''
    return {'argv': [sys.executable, '-u', '-c', code, mode, str(trigger)],
            'env': PEER_ENV.copy()}


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        assert time.monotonic() < deadline, 'owned process or evidence deadline expired'
        time.sleep(0.01)


class ControlRun:
    """One real CLI process tree, with stable handles only for that owned tree."""

    def __init__(self, session, modes, stop_timeout, max_restarts):
        self.session, self.stage = session, session / 'run'
        self.epoch = self.stage / 'epoch-0'
        self.trigger = session / 'release-peer'
        self.descendant_release = self.trigger.with_suffix('.child')
        self.pins, self.observations = {}, {}
        self.finished = False
        commands = [registered_control_command(mode, self.trigger) for mode in modes]
        config = session / 'commands.json'
        config.write_text(json.dumps({'commands': commands}))
        self.streams = [(session / ('cli.' + name)).open('w') for name in ('stdout', 'stderr')]
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name('run.py')), str(config),
             '--stage', str(self.stage), '--stop-timeout', str(stop_timeout),
             '--max-restarts', str(max_restarts)], stdout=self.streams[0], stderr=self.streams[1])

    def pin(self, pid, parent):
        """Pin an announced process and confirm its expected parent while alive."""
        if pid not in self.pins:
            self.pins[pid] = os.pidfd_open(pid)
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        assert int(fields[1]) == parent
        assert not select.select([self.pins[pid]], [], [], 0)[0]
        self.observations[str(pid)] = {'parent_pid': parent, 'start_time': fields[19]}

    def pin_ancestry(self):
        """Walk only the ready worker's ancestry to its known live CLI, before fault."""
        chain, pid = {}, self.worker
        while pid != self.process.pid:
            assert self.process.poll() is None
            assert pid not in chain and pid not in (0, 1, os.getpid())
            assert len(chain) < 8, 'unexpected sudo ancestry depth'
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            parent = int(fields[1])
            chain[pid] = parent
            pid = parent
        assert chain.get(self.wrapper) == self.process.pid
        for pid, parent in reversed(list(chain.items())):
            self.pin(pid, parent)

    def ready(self):
        self.pin(self.process.pid, os.getpid())
        wait_for(lambda: (self.epoch / 'ready.json').exists() or self.process.poll() is not None)
        assert self.process.poll() is None, self.session
        self.worker = json.loads((self.epoch / 'worker.json').read_text())['pid']
        self.wrapper = json.loads((self.epoch / 'launcher.json').read_text())['wrapper_pid']
        self.pin_ancestry()
        ready = json.loads((self.epoch / 'ready.json').read_text())
        self.peers, self.group_dir = ready['peers'], Path(ready['epoch_dir'])
        for index, peer in enumerate(self.peers):
            self.pin(peer, self.worker)
            wait_for(lambda index=index: (self.group_dir / f'peer-{index}.running').exists())
            descendant = self.group_dir / f'peer-{index}.descendant'
            if descendant.exists():
                child = int(descendant.read_text())
                self.pin(child, peer)
                state = self.group_dir / f'peer-{index}.descendant-state'
                wait_for(state.exists)
                assert json.loads(state.read_text()) == {'pid': child, 'parent_death_signal': 0}
        assert {self.worker, self.wrapper, *self.peers} <= self.pins.keys()
        assert_isolation(self.epoch, require_live=True)
        return self

    def send_signal(self, pid, signum):
        signal.pidfd_send_signal(self.pins[pid], signum)

    def finish(self):
        code = self.process.wait(timeout=35)
        if self.finished:
            return code
        for pid, fd in self.pins.items():
            assert select.select([fd], [], [], 15)[0], f'owned PID {pid} survived; {self.session}'
            observation = self.observations.setdefault(str(pid), {})
            observation['death_confirmed'] = True
            if pid == self.process.pid:
                observation['returncode'] = code
                continue
            try:
                waited, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                observation['reaped_by_other_owner'] = True
            else:
                assert waited == pid, 'pidfd signalled but adopted child was not waitable'
                observation['returncode'] = os.waitstatus_to_exitcode(status)
        (self.session / 'test-owned-processes.json').write_text(
            json.dumps(self.observations, indent=2) + '\n')
        assert sorted(path.name for path in self.stage.glob('epoch-*')) == ['epoch-0']
        self.finished = True
        return code


@pytest.fixture
def control_runs(tmp_path):
    """Reap only known adopted test processes after CLI/worker loss, never daemons."""
    libc = ctypes.CDLL(None, use_errno=True)
    prior = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(prior), 0, 0, 0) == 0  # GET_CHILD_SUBREAPER
    assert libc.prctl(36, 1, 0, 0, 0) == 0  # SET_CHILD_SUBREAPER
    runs, baseline = [], host_shm()
    root = Path(os.environ.get('G3_PEER_LOG_ROOT', tmp_path))
    root.mkdir(parents=True, exist_ok=True)

    def start(modes=('idle',), *, stop_timeout=5, max_restarts=1):
        session = Path(tempfile.mkdtemp(prefix='namespace-control-', dir=root)).resolve()
        run = ControlRun(session, modes, stop_timeout, max_restarts)
        runs.append(run)
        return run.ready()

    try:
        yield start
    finally:
        errors = []
        try:
            for run in reversed(runs):
                try:
                    if run.process.poll() is None:
                        run.process.terminate()
                    # A killed CLI may leave a worker until EOF is processed.
                    # Request ordinary cleanup if the assertion failed first.
                    worker = getattr(run, 'worker', None)
                    if worker in run.pins and not select.select([run.pins[worker]], [], [], 0)[0]:
                        run.send_signal(worker, signal.SIGTERM)
                    run.descendant_release.touch(exist_ok=True)
                    run.finish()
                except Exception as error:
                    errors.append(f'{run.session}: {error!r}')
                    # Clean up only synthetic direct peers pinned before the
                    # fault; the natural-exit child is never signalled.
                    try:
                        run.descendant_release.touch(exist_ok=True)
                        for pid in getattr(run, 'peers', []):
                            if pid in run.pins and not select.select([run.pins[pid]], [], [], 0)[0]:
                                run.send_signal(pid, signal.SIGKILL)
                        run.finish()
                    except Exception as cleanup_error:
                        errors.append(f'{run.session} cleanup: {cleanup_error!r}')
                finally:
                    for fd in run.pins.values():
                        os.close(fd)
                    for stream in run.streams:
                        stream.close()
        finally:
            assert libc.prctl(36, prior.value, 0, 0, 0) == 0
        assert host_shm() == baseline
        assert not errors, errors


def worker_result(run):
    return json.loads((run.epoch / 'worker-result.json').read_text())


def cli_result(run):
    result = json.loads((run.stage / 'cli.json').read_text())
    assert result['restarts'] == 0
    return result


def test_cli_sigterm_drains_actual_namespace_without_restart(control_runs):
    run = control_runs()
    run.send_signal(run.process.pid, signal.SIGTERM)
    assert run.finish() == 128 + signal.SIGTERM
    assert cli_result(run)['exit_code'] == 143
    result = worker_result(run)
    assert result['requested_stop'] and result['teardown_complete'] and result['echild']
    assert not result['peer_failed'] and not result['retry_allowed']
    assert result['descendants'] == [] and result['final_shm'] == {}
    owner = json.loads((run.epoch / 'owner.json').read_text())
    assert owner['worker_death_confirmed'] and owner['verified_teardown']


def test_cli_sigkill_eof_still_drains_actual_worker(control_runs):
    run = control_runs()
    run.send_signal(run.process.pid, signal.SIGKILL)
    assert run.finish() == -signal.SIGKILL
    result = worker_result(run)
    assert result['requested_stop'] and result['teardown_complete'] and result['echild']
    assert not result['retry_allowed'] and result['descendants'] == []
    assert not (run.stage / 'cli.json').exists()


def test_sudo_wrapper_death_does_not_adopt_worker_as_success(control_runs):
    run = control_runs()
    run.send_signal(run.wrapper, signal.SIGKILL)
    assert run.finish() == 1
    assert cli_result(run)['exit_code'] == 1
    result = worker_result(run)
    assert result['requested_stop'] and result['teardown_complete'] and result['echild']
    owner = json.loads((run.epoch / 'owner.json').read_text())
    assert owner['worker_death_confirmed'] and not owner['verified_teardown']


def test_worker_sigkill_kills_direct_peers_and_refuses_restart(control_runs):
    run = control_runs(('idle', 'idle'))
    run.send_signal(run.worker, signal.SIGKILL)
    assert run.finish() == 1
    assert cli_result(run)['exit_code'] == 1
    assert not (run.epoch / 'worker-result.json').exists()
    owner = json.loads((run.epoch / 'owner.json').read_text())
    assert owner['worker_death_confirmed'] and not owner['verified_teardown']
    for pid in run.peers:
        assert run.observations[str(pid)]['death_confirmed']
        assert run.observations[str(pid)]['returncode'] == -signal.SIGKILL


def test_sigkill_escalation_after_normal_peer_exit_is_cli_failure(control_runs):
    run = control_runs(('exit_when_released', 'ignore_term'), stop_timeout=0.2, max_restarts=0)
    run.trigger.touch()
    assert run.finish() == 1
    assert cli_result(run)['exit_code'] == 1
    result = worker_result(run)
    assert result['teardown_complete'] and result['echild'] and result['peer_failed']
    assert result['returncodes'] == [0, -signal.SIGKILL]
    group = json.loads((run.epoch / 'group/recovery.json').read_text())['epochs'][0]
    assert group['peers'][1]['signals'] == [signal.SIGTERM, signal.SIGKILL]


def test_descendant_drain_timeout_refuses_restart_without_signalling_child(control_runs):
    run = control_runs(('descendant',), stop_timeout=0.1)
    child = int((run.group_dir / 'peer-0.descendant').read_text())
    assert child in run.pins
    run.trigger.touch()
    assert run.process.wait(timeout=35) == 1
    assert not select.select([run.pins[child]], [], [], 0)[0]
    assert cli_result(run)['exit_code'] == 1
    result = worker_result(run)
    assert 'natural-exit deadline expired' in result['drain_error']
    assert not result['teardown_complete'] and not result['retry_allowed']
    assert not result.get('echild')
    run.descendant_release.touch()
    assert run.finish() == 1
    assert run.observations[str(child)]['returncode'] == 0
    owner = json.loads((run.epoch / 'owner.json').read_text())
    assert owner['worker_death_confirmed'] and not owner['verified_teardown']
