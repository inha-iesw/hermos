"""Peer protocol and process oracles shared by integration tests and diagnostics."""

import os
from pathlib import Path
import signal
import sys
import time


ROOT = Path(__file__).resolve().parents[3]
G3 = ROOT / 'build/g3/repair-probe/phase4_gpu_core_probe/repair/g3_transport_peer'


def launcher_argv(factory, *arguments, blocks=0, block_bytes=64, max_reads=2,
                  max_message_bytes=64):
    """Build the direct launcher command shared by these integration scenarios."""
    return [sys.executable, '-u', '-m', 'rclpy_agnocast_gpu.launch',
            '--blocks', str(blocks), '--block-bytes', str(block_bytes),
            '--max-reads', str(max_reads), '--max-message-bytes', str(max_message_bytes),
            factory, *map(str, arguments)]


def send_signal(pid, signum):
    """Signal a known owned peer through a pidfd and close that descriptor."""
    pidfd = os.pidfd_open(pid)
    try:
        signal.pidfd_send_signal(pidfd, signum)
    finally:
        os.close(pidfd)


def inventory():
    """Snapshot each host SHM entry once, including symlink identity."""
    return {path.name: (info.st_dev, info.st_ino, info.st_size)
            for path in Path('/dev/shm').iterdir() if (info := path.lstat())}


def successful(record):
    """Require peer success as well as verified namespace and descendant teardown."""
    assert record['verified_teardown'] and record['teardown_complete'] and record['echild'], record
    assert record['peer_failed'] is False, record
    assert record['returncodes'] and all(code == 0 for code in record['returncodes']), record
    assert all(item['returncode'] == 0 for item in record['descendants']), record


def environment():
    library = ROOT / 'build/g2-device-arm64/lib'
    return dict(AGNOCAST_DEVICE_PATH='/dev/agnocast_rclpy', AGNOCAST_BRIDGE_MODE='off',
                AGNOCAST_NO_DISCOVERY_AGENT='1',
                LD_PRELOAD=str(library / 'libagnocast_heaphook.so'),
                LD_LIBRARY_PATH=str(library) + ':' + os.environ.get('LD_LIBRARY_PATH', ''),
                PYTHONPATH=str(Path(__file__).parent) + ':' + os.environ.get('PYTHONPATH', ''))


def peer(language, role, topic, dtype='uint8'):
    if language == 'cpp':
        argv = [str(G3), role, topic, '1']
    else:
        argv = launcher_argv('tensor_peer:factory', role, topic, dtype,
                             blocks=1 if role == 'publisher' else 0)
    return {'argv': argv, 'env': environment()}


class Protocol:
    def __init__(self, epoch):
        self.epoch, self.offsets = epoch, [0, 0]

    def receive(self, index):
        path = self.epoch.epoch_dir / f'peer-{index}.stdout'
        deadline = time.monotonic() + 15
        events = {'READY', 'PUB', 'RECEIVED', 'MAPPED', 'DONE', 'FULL', 'HOLDING',
                  'DROPPED', 'BUSY', 'CLOSED', 'RECREATED', 'STATS'}
        while time.monotonic() < deadline:
            # A peer can be between separate writes from one print() call.
            # Leave the unterminated tail unread until its newline arrives.
            lines = path.read_text().split('\n')[:-1]
            while self.offsets[index] < len(lines):
                line = lines[self.offsets[index]]
                self.offsets[index] += 1
                if line and line.split()[0] in events:
                    return line
            if self.epoch.poll() is not None:
                raise AssertionError(f'peer exited; inspect {self.epoch.epoch_dir}')
            time.sleep(0.005)
        raise AssertionError(f'peer response timed out; inspect {path}')

    def ask(self, index, command):
        self.epoch.send(index, command)
        return self.receive(index)

    def publish(self, value):
        deadline = time.monotonic() + 3
        while True:
            answer = self.ask(0, f'publish {value}')
            if answer.startswith('PUB '):
                return answer
            assert answer == 'FULL' and time.monotonic() < deadline
            time.sleep(0.01)
