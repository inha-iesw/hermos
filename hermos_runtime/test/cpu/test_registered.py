"""Registered CPU factories cannot dispatch before the supervisor ACK."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from hermos_runtime.cpu import registered


@pytest.fixture
def application(monkeypatch):
    events = []
    state = SimpleNamespace(failure=None, events=events)

    class Context:
        def __init__(self):
            self.active = True

        def ok(self):
            return self.active

        def shutdown(self):
            events.append('context-close')
            self.active = False
            if state.failure == 'context-close':
                raise RuntimeError('context close failed')

    class Node:
        def __init__(self, context):
            self.context = context

        def destroy_node(self):
            events.append('node-close')
            if state.failure == 'node-close':
                raise RuntimeError('node close failed')

    class Executor:
        def __init__(self, *, context):
            self.context = context

        def add_node(self, node):
            assert node.context is self.context
            events.append('add-node')

        def spin_once(self, *, timeout_sec):
            events.append('spin')
            if state.failure == 'spin':
                raise RuntimeError('callback failed')
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        def shutdown(self, *, timeout_sec):
            assert timeout_sec == 0
            events.append('executor-close')
            if state.failure == 'executor-close':
                raise RuntimeError('executor close failed')
            return state.failure != 'incomplete'

    def initialise(*, args, context, signal_handler_options):
        events.append('init')
        state.ros_args = args
        assert signal_handler_options == 'NO'

    def factory(context, args):
        events.append('factory')
        state.application_args = args
        if state.failure == 'factory':
            raise RuntimeError('factory failed')
        if state.failure == 'invalid':
            return [Node(context), object()]
        if state.failure == 'native-handler':
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        return Node(context)

    class Registration:
        @classmethod
        def from_environment(cls, *, required):
            assert required is True
            events.append('registration-check')
            if state.failure == 'environment':
                raise RuntimeError('supervisor registration is required')
            return cls()

        def wait(self, resources):
            assert resources == []
            assert 'factory' in events and 'spin' not in events
            events.append('ack')
            if state.failure == 'ack':
                raise TimeoutError('registration acknowledgement timed out')

    def remove_ros_args(args):
        return args[:args.index('--ros-args')] if '--ros-args' in args else args

    monkeypatch.setitem(sys.modules, 'rclpy', SimpleNamespace(init=initialise))
    monkeypatch.setitem(sys.modules, 'rclpy.context', SimpleNamespace(Context=Context))
    monkeypatch.setitem(sys.modules, 'rclpy.node', SimpleNamespace(Node=Node))
    monkeypatch.setitem(sys.modules, 'rclpy.executors', SimpleNamespace(
        SingleThreadedExecutor=Executor, ExternalShutdownException=type('ExternalShutdownException', (Exception,), {})))
    monkeypatch.setitem(sys.modules, 'rclpy.signals', SimpleNamespace(
        SignalHandlerOptions=SimpleNamespace(NO='NO')))
    monkeypatch.setitem(sys.modules, 'rclpy.utilities', SimpleNamespace(remove_ros_args=remove_ros_args))
    monkeypatch.setitem(sys.modules, 'hermos_runtime.supervision.registration',
                        SimpleNamespace(Registration=Registration))
    monkeypatch.setitem(sys.modules, 'registered_factory', SimpleNamespace(create=factory))
    return state


@pytest.mark.parametrize('failure', [None, 'native-handler'])
def test_factory_ack_dispatch_and_cleanup_are_ordered(application, failure):
    application.failure = failure
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    assert registered.main(['registered_factory:create', '--label', 'CPU',
                            '--ros-args', '-r', '__ns:=/pipeline']) == 0
    assert application.events == ['registration-check', 'init', 'factory', 'add-node',
                                  'ack', 'spin', 'executor-close', 'node-close', 'context-close']
    assert application.application_args == ['--label', 'CPU']
    assert application.ros_args == ['--label', 'CPU', '--ros-args', '-r', '__ns:=/pipeline']
    assert {number: signal.getsignal(number) for number in previous} == previous


@pytest.mark.parametrize('failure', ['factory', 'invalid', 'ack'])
def test_startup_failure_never_dispatches_and_closes_context(application, failure):
    application.failure = failure
    assert registered.main(['registered_factory:create']) == 70
    assert 'spin' not in application.events
    assert application.events[-1] == 'context-close'
    if failure != 'factory':
        assert 'node-close' in application.events


def test_missing_registration_fails_before_ros_or_application_initialisation(application):
    application.failure = 'environment'
    assert registered.main(['registered_factory:create']) == 70
    assert application.events == ['registration-check']


@pytest.mark.parametrize('failure', ['spin', 'incomplete', 'executor-close', 'node-close', 'context-close'])
def test_callback_or_cleanup_failure_reports_nonzero_and_attempts_remaining_cleanup(application, failure):
    application.failure = failure
    assert registered.main(['registered_factory:create']) == 70
    assert application.events[-3:] == ['executor-close', 'node-close', 'context-close']


def test_real_dds_factory_waits_for_ack_and_receives_ros_arguments(tmp_path):
    pytest.importorskip('rclpy')
    source = tmp_path / 'installed_factory.py'
    source.write_text('''
import json
import os
from pathlib import Path
import signal
from rclpy.node import Node

def create(context, args):
    node = Node('cpu_peer', context=context)
    label = node.declare_parameter('label', 'default').value
    output = Path(args[0])
    def dispatch():
        output.write_text(json.dumps(dict(name=node.get_name(), namespace=node.get_namespace(), label=label)))
        os.kill(os.getpid(), signal.SIGTERM)
    node.create_timer(0.01, dispatch)
    return node
''')
    manifest, ack, dispatched = (tmp_path / name for name in ('manifest.json', 'ack', 'dispatched.json'))
    environment = dict(os.environ)
    environment.pop('LD_PRELOAD', None)
    package = str(Path(registered.__file__).resolve().parents[2])
    environment.update(G3_RECOVERY_MANIFEST=str(manifest), G3_RECOVERY_ACK=str(ack),
                       G3_RECOVERY_START_TIMEOUT='5', ROS_LOCALHOST_ONLY='1',
                       ROS_DOMAIN_ID=str(100 + os.getpid() % 100),
                       PYTHONPATH=os.pathsep.join((str(tmp_path), package, environment.get('PYTHONPATH', ''))))
    process = subprocess.Popen([sys.executable, '-m', 'hermos_runtime.cpu.registered',
        'installed_factory:create', str(dispatched), '--ros-args', '-r', '__node:=registered_cpu',
        '-r', '__ns:=/pipeline', '-p', 'label:=CPU'], env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while not manifest.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert manifest.exists(), process.communicate(timeout=1)
        assert json.loads(manifest.read_text()) == dict(version=1, pid=process.pid, resources=[])
        time.sleep(0.1)
        assert not dispatched.exists()
        ack.touch()
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, (stdout, stderr)
        assert json.loads(dispatched.read_text()) == dict(
            name='registered_cpu', namespace='/pipeline', label='CPU')
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
