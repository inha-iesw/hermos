"""A terminal GPU error or stalled handoff must become a process failure."""

import os
from pathlib import Path
import signal
import tempfile
import time

import pytest

from support import ROOT, launcher_argv, send_signal


def command(scenario, marker, socket_path):
    env = dict(HERMOS_BRIDGE_MODE='off', HERMOS_NO_DISCOVERY_AGENT='1',
               PYTHONPATH=str(Path(__file__).parent) + ':' + os.environ.get('PYTHONPATH', ''))
    env['G4_STALL_SOCKET'] = str(socket_path)
    if scenario == 'poison':
        env['LD_PRELOAD'] = os.environ.get(
            'G4_FAULT_LIBRARY', str(ROOT / 'build/hermos/gpu-test/rclpy_hermos_gpu/libg4_query_failure.so'))
        env['G4_QUERY_FAILURE_MARKER'] = str(marker)
    return {'argv': launcher_argv('control_peer:factory', scenario, marker, blocks=1),
            'env': env}


@pytest.mark.parametrize('scenario', ['poison', 'stall'])
def test_failure_exits_without_supervisor_kill(epochs, tmp_path, scenario):
    with tempfile.TemporaryDirectory(prefix='g4-fd-', dir='/tmp') as sockets:
        check_failure(epochs, tmp_path, scenario, Path(sockets) / 'handoff.sock')


def check_failure(epochs, tmp_path, scenario, socket_path):
    marker = tmp_path / 'control'
    epoch = epochs([command(scenario, marker, socket_path)])
    deadline = time.monotonic() + 10
    while not marker.exists():
        assert epoch.poll() is None and time.monotonic() < deadline, epoch.epoch_dir
        time.sleep(0.005)
    started = time.monotonic()
    if scenario == 'stall':
        send_signal(epoch.peers[0], signal.SIGTERM)
    record = epoch.wait(timeout=10)
    elapsed = time.monotonic() - started
    assert record['verified_teardown'] and record['peer_failed'], record
    assert record['returncodes'] == [70], record
    assert record['echild'] and record['cleanup_complete'], record
    stderr = (epoch.epoch_dir / 'peer-0.stderr').read_text()
    if scenario == 'stall':
        assert elapsed < 5
        # Linux can interrupt recvmsg immediately; a non-interruptible native
        # call instead takes the watchdog boundary (covered separately on host).
        assert 'GPU read rejected' in stderr or 'TERM deadline expired' in stderr
    else:
        assert 'poisoned' in stderr
