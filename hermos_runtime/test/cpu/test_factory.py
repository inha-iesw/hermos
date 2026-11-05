"""CPU/GPU parity for the shared factory, context and partial-cleanup contract."""

import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture(params=['cpu', 'gpu'])
def peer_launcher(request, monkeypatch):
    if request.param == 'cpu':
        from hermos_runtime.cpu import registered as launcher
        prefix = []
    else:
        launcher = pytest.importorskip('rclpy_hermos_gpu.launch')
        prefix = ['--block-bytes', '64', '--max-reads', '2', '--max-message-bytes', '64']
    state = SimpleNamespace(kind=request.param, failure=None, events=[], launcher=launcher,
                            prefix=prefix, nodes=[], context=None)
    events = state.events

    class ExternalShutdownException(Exception):
        pass

    class Context:
        def __init__(self):
            self.active = True
            state.context = self

        def ok(self):
            return self.active

        def shutdown(self):
            events.append('context-close')
            self.active = False
            if state.failure == 'context-close':
                raise RuntimeError('context shutdown failed')

    class Node:
        def __init__(self, context):
            self.context = context
            state.nodes.append(self)

        def destroy_node(self):
            events.append('node-close')
            if state.failure == 'node-close':
                raise RuntimeError('node shutdown failed')

    class Executor:
        def __init__(self, *, context):
            self.context = context

        def add_node(self, node):
            events.append('add-node')

        def spin_once(self, *, timeout_sec):
            events.append('spin')
            if state.failure in ('external-down', 'external-live'):
                self.context.active = state.failure == 'external-live'
                raise ExternalShutdownException()
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        def begin_drain(self):
            events.append('begin-drain')

        def progress(self):
            pass

        def shutdown(self, *, timeout_sec):
            events.append('executor-close')
            if state.failure == 'executor-close':
                raise RuntimeError('executor shutdown failed')
            return state.failure != 'incomplete'

    class Registration:
        @classmethod
        def from_environment(cls, *, required):
            events.append('registration-check')
            return cls()

        def wait(self, resources):
            events.append('ack')

    class Runtime:
        def __init__(self, **configuration):
            pass

        def initialise(self):
            events.append('runtime-init')

        def register(self):
            events.append('ack')

        def poll(self):
            events.append('poll')

        def shutdown(self, *, timeout, progress):
            events.append('runtime-close')

    class Deadline:
        def __init__(self):
            events.append('deadline-start')

        def close(self):
            events.append('deadline-close')

    def initialise(*, args, context, signal_handler_options):
        events.append('init')
        if state.failure == 'init':
            raise RuntimeError('context initialization failed after allocation')

    def application(owner, arguments):
        events.append('factory')
        context = owner if state.kind == 'cpu' else owner.ros_context
        if state.failure == 'empty':
            return []
        node = Node(context)
        if state.failure == 'wrong-context':
            return [node, Node(object())]
        if state.failure == 'invalid':
            return [node, object(), Node(context)]
        if state.failure == 'iterator':
            def partial():
                yield node
                raise RuntimeError('factory iterator failed after yielding a node')
            return partial()
        return node

    modules = {
        'rclpy': SimpleNamespace(init=initialise),
        'rclpy.context': SimpleNamespace(Context=Context),
        'rclpy.node': SimpleNamespace(Node=Node),
        'rclpy.executors': SimpleNamespace(SingleThreadedExecutor=Executor,
                                         ExternalShutdownException=ExternalShutdownException),
        'rclpy.signals': SimpleNamespace(SignalHandlerOptions=SimpleNamespace(NO='NO')),
        'rclpy.utilities': SimpleNamespace(remove_ros_args=lambda args: args),
        'hermos_runtime.supervision.registration': SimpleNamespace(Registration=Registration),
        'contract_application': SimpleNamespace(create=application),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    if state.kind == 'gpu':
        from rclpy_hermos_gpu import runtime
        monkeypatch.setattr(runtime, 'GpuRuntime', Runtime)
        monkeypatch.setattr(launcher, '_draining_executor', Executor)
        monkeypatch.setattr(launcher, '_TerminationDeadline', Deadline)
    return state


@pytest.mark.parametrize('failure', ['wrong-context', 'invalid', 'iterator', 'empty', 'init'])
def test_invalid_factory_or_initialisation_cleans_partial_ownership_without_dispatch(peer_launcher, failure):
    peer_launcher.failure = failure
    result = peer_launcher.launcher.main([*peer_launcher.prefix, 'contract_application:create'])
    assert result == 70
    assert 'ack' not in peer_launcher.events and 'spin' not in peer_launcher.events
    assert peer_launcher.events.count('node-close') == len(peer_launcher.nodes)
    assert 'context-close' in peer_launcher.events


@pytest.mark.parametrize('failure,expected', [
    (None, 0), ('external-down', 0), ('external-live', 70), ('incomplete', 70),
    ('executor-close', 70), ('node-close', 70), ('context-close', 70),
])
def test_dispatch_and_cleanup_status_parity_and_signal_restoration(peer_launcher, failure, expected):
    peer_launcher.failure = failure
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        result = peer_launcher.launcher.main([*peer_launcher.prefix, 'contract_application:create'])
        assert result == expected
        assert {number: signal.getsignal(number) for number in previous} == previous
    finally:
        # A failing implementation must not leave pytest with SIGTERM ignored.
        for number, handler in previous.items():
            signal.signal(number, handler)
    events = peer_launcher.events
    assert events.index('ack') < events.index('spin')
    assert 'executor-close' in events and 'node-close' in events
    if failure != 'external-down':
        assert 'context-close' in events
    if peer_launcher.kind == 'gpu':
        assert events.index('runtime-close') < events.index('executor-close')
        assert events[-1] == 'deadline-close'


@pytest.mark.parametrize('spec', ['', 'factory', ':create', 'module:', 'module::create',
                                 'bad-module:create', 'module.bad-name:create', 'module:bad-name'])
def test_factory_syntax_is_rejected_before_any_ros_or_gpu_initialisation(peer_launcher, spec):
    with pytest.raises(SystemExit) as caught:
        peer_launcher.launcher.main([*peer_launcher.prefix, spec])
    assert caught.value.code == 2
    assert peer_launcher.events == []


def test_shared_factory_contract_imports_without_ros_native_or_torch():
    environment = dict(os.environ)
    source = str(Path(__file__).resolve().parents[2])
    environment['PYTHONPATH'] = source
    code = '''
import sys
from hermos_runtime.supervision.factory import parse_factory
assert parse_factory('example.pipeline:create') == ('example.pipeline', 'create')
assert not any(name.split('.')[0] in {'rclpy', 'rclpy_hermos', 'rclpy_hermos_gpu', 'torch'}
               for name in sys.modules)
'''
    result = subprocess.run([sys.executable, '-B', '-c', code], env=environment,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
