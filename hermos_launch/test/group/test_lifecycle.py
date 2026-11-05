"""Kill only owned launch processes and verify supervisor-driven peer drain."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time


@contextmanager
def launched_group(tmp_path, *, delay=0, mode='parent-death'):
    prefix = tmp_path / 'prefix'
    marker = prefix / 'share/ament_index/resource_index/packages/host_peers'
    marker.parent.mkdir(parents=True)
    marker.touch()
    executable = prefix / 'lib/host_peers/probe'
    executable.parent.mkdir(parents=True)
    executable.symlink_to(Path(__file__).with_name('registered_probe.py'))
    environment = dict(os.environ, ROS_LOG_DIR=str(tmp_path / 'ros-log'))
    environment.pop('LD_PRELOAD', None)
    handles = []
    with (tmp_path / 'launch.log').open('w') as log:
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('host_launch.py')),
                                    str(tmp_path), str(delay), mode], env=environment,
                                   stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 15
            while not (tmp_path / 'peer.json').exists():
                assert process.poll() is None, (tmp_path / 'launch.log').read_text()
                assert time.monotonic() < deadline, (tmp_path / 'launch.log').read_text()
                time.sleep(0.01)
            record = json.loads((tmp_path / 'peer.json').read_text())
            handles = [os.pidfd_open(record[key]) for key in ('supervisor', 'pid')]
            yield process, handles
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
            process.wait(timeout=5)
            for handle in handles:
                if not select.select([handle], [], [], 2)[0]:
                    signal.pidfd_send_signal(handle, signal.SIGKILL)
                os.close(handle)


def test_launch_sigint_allows_peer_drain_beyond_default_five_second_timeout(tmp_path):
    with launched_group(tmp_path, delay=6, mode='shutdown') as (process, handles):
        assert process.wait(timeout=20) == 0, (tmp_path / 'launch.log').read_text()
        assert all(select.select([handle], [], [], 0)[0] for handle in handles)
    summary = json.loads(next(tmp_path.glob('hermos-group-*/supervisor/cli.json')).read_text())
    assert summary['exit_code'] == 130 and summary['verified_teardown']
    assert (tmp_path / 'peer.drained').exists()


def test_launch_parent_sigkill_requests_verified_supervisor_drain(tmp_path):
    with launched_group(tmp_path) as (process, handles):
        process.kill()
        assert process.wait(timeout=5) == -signal.SIGKILL
        assert all(select.select([handle], [], [], 10)[0] for handle in handles)
    summary = json.loads(next(tmp_path.glob('hermos-group-*/supervisor/cli.json')).read_text())
    assert summary['exit_code'] == 143 and summary['verified_teardown']
    assert summary['state'] == 'stopped' and (tmp_path / 'peer.drained').exists()


def test_sigint_sent_only_to_launch_pid_starts_immediate_drain(tmp_path):
    if os.environ.get('HERMOS_TEST_TTY') == '1':
        assert os.isatty(0), 'run this check with a terminal and pytest -s'
    with launched_group(tmp_path, mode='parent-sigint') as (process, handles):
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=4) == 0, (tmp_path / 'launch.log').read_text()
        assert all(select.select([handle], [], [], 0)[0] for handle in handles)
    summary = json.loads(next(tmp_path.glob('hermos-group-*/supervisor/cli.json')).read_text())
    assert summary['exit_code'] == 130 and summary['verified_teardown']
    assert not summary['peer_failed'] and (tmp_path / 'peer.drained').exists()


def test_shutdown_peer_failure_is_not_hidden_by_confirmed_process_cleanup(tmp_path):
    with launched_group(tmp_path, mode='shutdown-failure') as (process, handles):
        assert process.wait(timeout=15) != 0
        assert all(select.select([handle], [], [], 0)[0] for handle in handles)
    summary = json.loads(next(tmp_path.glob('hermos-group-*/supervisor/cli.json')).read_text())
    assert summary['exit_code'] == 130 and summary['verified_teardown'] and summary['peer_failed']
