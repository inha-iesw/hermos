"""Exercise whole-group recovery with the actual Hermos/CUDA peers."""

import json
import os
from pathlib import Path
import select
import signal
import tempfile
import time

import pytest

from hermos_runtime.supervision.group import Group


pytestmark = pytest.mark.skipif(
    os.environ.get('G3_RUN_TRANSPORT') != '1', reason='requires verified device session')


class Pair:
    def __init__(self, stage, blocks, fault=None):
        binary = os.environ['G3_TRANSPORT_PEER']
        hook = Path(os.environ.get('RCLPY_HERMOS_DEVICE_BUNDLE',
            Path(__file__).resolve().parents[3] / 'build/hermos/device-arm64')) / 'lib'
        env = dict(HERMOS_DEVICE_PATH='/dev/hermos_rclpy', HERMOS_BRIDGE_MODE='off',
                   HERMOS_NO_DISCOVERY_AGENT='1',
                   LD_PRELOAD=str(hook / 'libhermos_heaphook.so'),
                   LD_LIBRARY_PATH=str(hook) + ':' + os.environ.get('LD_LIBRARY_PATH', ''))
        topic = f'/g3_recovery_{os.getpid()}_{time.monotonic_ns()}'
        commands = []
        for role, limit in (('publisher', blocks), ('subscriber', 1)):
            peer_env = env.copy()
            peer_binary = binary
            if role == 'subscriber' and fault is not None:
                peer_binary = os.environ['G3_CLEANUP_PEER']
                peer_env['G3_IMPORT_FAILURE'] = fault
            commands.append({'argv': [peer_binary, role, topic, str(limit)], 'env': peer_env})
        self.group = Group(commands, stage, startup_timeout=10, stop_timeout=3)
        try:
            self.group.start()
            self.ready()
        except BaseException:
            self.group.stop('peer readiness failed')
            raise

    def ready(self):
        self.offsets = [0, 0]
        assert self.receive(0)[0] == 'READY'
        assert self.receive(1)[0] == 'READY'
        deadline = time.monotonic() + 5
        while self.ask(0, 'connections') != ['CONNECTIONS', '1']:
            assert time.monotonic() < deadline
            time.sleep(0.01)

    def receive(self, peer):
        log = self.group.epoch_dir / f'peer-{peer}.stdout'
        deadline = time.monotonic() + 8
        events = {'READY', 'PUB', 'MAPPED', 'FULL', 'DONE', 'CONNECTIONS', 'CLOSED',
                  'STASHED', 'POISONED', 'DROPPED', 'FAULT_STATS', 'READ_CLOSED'}
        while time.monotonic() < deadline:
            lines = log.read_text().splitlines()
            while self.offsets[peer] < len(lines):
                line = lines[self.offsets[peer]]
                self.offsets[peer] += 1
                tokens = line.split()
                if tokens and tokens[0] in events:
                    return tokens
            time.sleep(0.005)
        raise AssertionError(f'peer {peer} response timed out; see {log}')

    def ask(self, peer, command):
        stream = self.group.processes[peer].stdin
        stream.write(command + '\n')
        stream.flush()
        return self.receive(peer)

    def publish(self, value):
        deadline = time.monotonic() + 3
        while True:
            event = self.ask(0, f'publish {value}')
            if event[0] == 'PUB':
                return event
            assert event == ['FULL'] and time.monotonic() < deadline
            time.sleep(0.01)

    def checksum(self, value):
        packet = self.publish(value)
        assert self.ask(1, 'take')[:2] == ['MAPPED', packet[1]]
        assert self.ask(1, 'finish') == ['DONE', str(value * 64)]


@pytest.fixture
def pairs(tmp_path):
    root = Path(os.environ.get('G3_PEER_LOG_ROOT', tmp_path))
    root.mkdir(parents=True, exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix='recovery-', dir=root))
    active = []

    def start(blocks=1, fault=None):
        pair = Pair(session / f'group-{len(active)}', blocks, fault)
        active.append(pair)
        return pair

    yield start
    for pair in reversed(active):
        pair.group.stop('test teardown')


def test_registered_unpublished_blocks_are_cleaned_only_after_group_death(pairs):
    pair = pairs(blocks=3)
    manifest = json.loads((pair.group.epoch_dir / 'peer-0.json').read_text())
    paths = [Path(path) for path in manifest['resources']]
    assert len(paths) == 6
    assert all(path.exists() for path in paths)
    assert json.loads((pair.group.epoch_dir / 'peer-1.json').read_text())['resources'] == []
    pair.group.stop('unpublished pool shutdown')
    assert all(not path.exists() for path in paths)
    epoch = json.loads((pair.group.stage / 'recovery.json').read_text())['epochs'][-1]
    assert epoch['registered'] and epoch['confirmed_dead']
    assert epoch['cleanup'] == 'complete'


def test_reader_crash_restarts_registered_group_and_preserves_independent_pair(pairs):
    healthy, crashed = pairs(), pairs()
    for cycle in range(3):
        packet = crashed.publish(31 + cycle)
        assert crashed.ask(1, 'take')[:2] == ['MAPPED', packet[1]]
        previous = list(crashed.group.processes)
        manifest = json.loads((crashed.group.epoch_dir / 'peer-0.json').read_text())
        paths = [Path(path) for path in manifest['resources']]
        # Inject a crash through a stable process handle; Group remains the sole reaper.
        pidfd = os.pidfd_open(previous[1].pid)
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        finally:
            os.close(pidfd)
        time.sleep(0.15)
        assert crashed.ask(0, 'probe') == ['FULL']
        assert all(path.exists() for path in paths)
        healthy.checksum(71 + cycle)
        crashed.group.restart('injected reader crash')
        assert all(process.returncode is not None for process in previous)
        assert all(not path.exists() for path in paths)
        crashed.ready()
        crashed.checksum(41 + cycle)
    evidence = json.loads((crashed.group.stage / 'recovery.json').read_text())
    assert len(evidence['epochs']) == 4
    assert all(epoch['confirmed_dead'] and epoch['cleanup'] == 'complete'
               for epoch in evidence['epochs'][:-1])


@pytest.mark.parametrize('fault', ['partial_unmap', 'event_destroy'])
def test_terminal_poison_exit_restarts_group_and_preserves_independent_pair(pairs, fault):
    healthy, poisoned = pairs(), pairs(fault=fault)
    healthy_pids = [process.pid for process in healthy.group.processes]
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
    assert len(stats) == 8
    assert stats[1:4] == ['1', '1', '1']  # One injected cleanup failure after real import.
    assert int(stats[4]) >= 1
    assert poisoned.ask(1, 'close_read') == ['READ_CLOSED', '0']
    assert poisoned.ask(0, 'probe') == ['FULL']
    healthy.checksum(79)

    previous = list(poisoned.group.processes)
    prior_epoch = poisoned.group.epoch_dir
    paths = [Path(path) for index in range(2)
             for path in json.loads((prior_epoch / f'peer-{index}.json').read_text())['resources']]
    assert len(paths) == 2 and all(path.exists() for path in paths)
    # Observe death without reaping: the failed drain exits with code 1, and
    # Group remains the sole wait owner for the complete publisher/reader epoch.
    pidfd = os.pidfd_open(previous[1].pid)
    try:
        previous[1].stdin.write('quit\n')
        previous[1].stdin.flush()
        deaths = select.poll()
        deaths.register(pidfd, select.POLLIN)
        assert any(fd == pidfd and flags & select.POLLIN for fd, flags in deaths.poll(8000))
    finally:
        os.close(pidfd)
    assert all(path.exists() for path in paths)
    assert 'GPU subscriber did not drain; peer restart required' in (
        prior_epoch / 'peer-1.stderr').read_text()
    healthy.checksum(83)

    # Disable only the test injector for the fresh process epoch. This is not
    # recovery of a live poisoned mapper, and no reference/FD reset is performed.
    poisoned.group.commands[1]['env'].pop('G3_IMPORT_FAILURE')
    poisoned.group.restart('terminal poison reported by reader exit')
    assert previous[1].returncode == 1
    assert all(process.returncode is not None for process in previous)
    assert all(not path.exists() for path in paths)
    history = json.loads((poisoned.group.stage / 'recovery.json').read_text())['epochs']
    assert len(history) == 2
    assert history[0]['confirmed_dead'] and history[0]['cleanup'] == 'complete'
    assert history[0]['returncodes'][1] == 1
    poisoned.ready()
    poisoned.checksum(59)
    fresh = poisoned.ask(1, 'fault_stats')
    assert fresh[1] == fresh[-1] == '0'
    healthy.checksum(89)
    assert [process.pid for process in healthy.group.processes] == healthy_pids
