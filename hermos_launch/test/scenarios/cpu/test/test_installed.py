"""Actual ros2 run/launch checks in a relocated, source-independent CPU installation."""

import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time

import pytest

from installation import CopiedInstallation


pytestmark = pytest.mark.skipif(os.environ.get('HERMOS_ECOSYSTEM_RUN_DEVICE') != '1',
    reason='requires an owned hermos_rclpy device session and installed CPU artifacts')


@pytest.fixture(scope='module')
def installed():
    installation = CopiedInstallation()
    yield installation
    installation.close()


class Peer:
    def __init__(self, installed, command, evidence):
        self.events = []
        self.lines = []
        self.queue = queue.Queue()
        self.evidence = evidence
        self.process = subprocess.Popen(command, cwd=installed.root, env=installed.environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            self.lines.append(line)
            marker = 'HERMOS_EXAMPLE '
            if marker in line:
                event = json.loads(line.split(marker, 1)[1])
                self.events.append(event)
                self.queue.put(event)
        self.queue.put(None)

    def event(self, kind, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = self.queue.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if event is None:
                break
            if event['event'] == kind:
                return event
        raise AssertionError(f'missing {kind}: {"".join(self.lines)}')

    def finish(self):
        result = self.process.wait(timeout=20)
        self.reader.join(timeout=2)
        self.evidence.parent.mkdir(parents=True, exist_ok=True)
        self.evidence.write_text(''.join(self.lines))
        assert result == 0, ''.join(self.lines)
        assert any(event['event'] == 'closed' for event in self.events), ''.join(self.lines)
        return next(event for event in self.events if event['event'] == 'done')

    def close(self):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGINT)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        self.evidence.parent.mkdir(parents=True, exist_ok=True)
        self.evidence.write_text(''.join(self.lines))


@pytest.fixture
def peers(installed, request, tmp_path):
    active = []
    evidence = Path(os.environ.get('HERMOS_ECOSYSTEM_EVIDENCE_DIR', str(tmp_path)))

    def start(arguments):
        path = evidence / request.node.name / f'peer-{len(active)}.log'
        peer = Peer(installed, ['/opt/ros/humble/bin/ros2', *arguments], path)
        active.append(peer)
        return peer

    yield start
    for peer in reversed(active):
        peer.close()


def run_args(executable, role=None, count=6):
    args = ['run', 'hermos_cpu_examples', executable]
    if role:
        args += ['--role', role, '--count', str(count), '--tag', '실제 argv with spaces']
    args += ['--ros-args', '-r', '__ns:=/installed', '-r', 'image:=frames',
             '-p', 'label:=검증 parameter with spaces']
    if not role:
        args += ['-p', f'count:={count}']
    return args


def assert_relocated(ready, installed):
    assert str(installed.root) in ready['malloc_provider']
    assert ready['loaded'] and all('/workspace/' not in path for path in ready['loaded'])
    assert ready['namespace'] == '/installed'
    assert ready['label'] == '검증 parameter with spaces'


@pytest.mark.parametrize('subscriber', ['image_peer', 'cpp_image_reader'])
def test_separate_ros2_run_delivers_full_images(installed, peers, subscriber):
    reader = peers(run_args(subscriber, 'subscriber' if subscriber == 'image_peer' else None))
    ready = reader.event('ready')
    assert_relocated(ready, installed)
    if subscriber == 'image_peer':
        assert ready['topic'] == '/installed/frames'
        result = subprocess.run(['/opt/ros/humble/bin/ros2', 'param', 'get',
            '/installed/image_subscriber', 'label'], env=installed.environment,
            cwd=installed.root, capture_output=True, text=True, timeout=8)
        assert result.returncode == 0, result.stderr
        assert '검증 parameter with spaces' in result.stdout
    writer = peers(run_args('image_peer', 'publisher'))
    written = writer.event('ready')
    assert_relocated(written, installed)
    assert written['tag'] == '실제 argv with spaces'
    assert written['topic'] == '/installed/frames'
    assert writer.finish()['count'] == 6
    result = reader.finish()
    assert result['count'] == 6
    if subscriber == 'image_peer':
        assert result['dds_received'] > 0


def test_ros2_launch_uses_installed_yaml_remaps_and_arguments(installed, peers):
    pipeline = peers(['launch', 'hermos_cpu_examples', 'cpu_pipeline.launch.py',
                      'namespace:=launched'])
    pipeline.finish()
    ready = [event for event in pipeline.events if event['event'] == 'ready']
    done = [event for event in pipeline.events if event['event'] == 'done']
    assert len(ready) == len(done) == 2
    assert {event['name'] for event in ready} == {'reader', 'writer'}
    assert all(event['namespace'] == '/launched' and event['topic'] == '/launched/frames'
               and event['label'] == '설치 예제 label with spaces' for event in ready)
    assert {event['tag'] for event in ready} == {'reader 공백 argument', 'writer 공백 argument'}
    assert all(event['count'] == 6 for event in done)
    assert all('/workspace/' not in path for event in ready for path in event['loaded'])


def test_cpp_publisher_to_python_subscriber(installed, peers):
    reader = peers(run_args('image_peer', 'subscriber'))
    reader.event('ready')
    writer = peers([*run_args('cpp_image_peer'), '-p', 'role:=publisher'])
    assert_relocated(writer.event('ready'), installed)
    assert writer.finish()['count'] == 6
    assert reader.finish()['count'] == 6


@pytest.mark.parametrize('subscriber', ['image_peer', 'cpp_image_reader'])
def test_ctrl_c_closes_installed_peer(installed, peers, subscriber):
    reader = peers(run_args(subscriber, 'subscriber' if subscriber == 'image_peer' else None))
    reader.event('ready')
    os.killpg(reader.process.pid, signal.SIGINT)
    result = reader.finish()
    assert result['signal'] == signal.SIGINT and result['count'] == 0


def test_subscriber_first_shutdown_preserves_publisher_completion(installed, peers):
    reader = peers(run_args('image_peer', 'subscriber', count=2))
    reader.event('ready')
    writer = peers(run_args('image_peer', 'publisher', count=12))
    writer.event('ready')
    assert reader.finish()['count'] == 2
    assert writer.finish()['count'] == 12
