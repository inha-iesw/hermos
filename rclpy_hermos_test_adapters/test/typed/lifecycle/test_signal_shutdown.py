"""Exercise the stock Humble WaitSet/context race without the shared-heap device."""

from types import SimpleNamespace
import threading

import pytest
import rclpy
from rclpy.context import Context
from rclpy.executors import ExternalShutdownException
from rclpy.impl.implementation_singleton import rclpy_implementation as _rclpy
from rclpy.node import Node

import facade_process


INVALID_WAIT_SET = ('failed to initialize wait set: the given context is not valid, '
                    'at ./src/rcl/wait.c:130')


def test_signal_shutdown_between_context_check_and_waitset_creation(monkeypatch):
    """A real native WaitSet failure during intended shutdown still permits cleanup."""
    context = Context()
    rclpy.init(context=context)
    node = Node('controlled_signal_shutdown', context=context)
    requested = threading.Event()
    constructed = threading.Event()
    events = []
    native_wait_set = _rclpy.WaitSet

    def racing_wait_set(*args, **kwargs):
        assert requested.wait(5), 'peer did not enter signal wait'
        context.shutdown()
        constructed.set()
        return native_wait_set(*args, **kwargs)

    def wait_signal(observed_node):
        assert observed_node is node
        requested.set()
        assert constructed.wait(5), 'executor did not reach WaitSet construction'
        assert not context.ok()

    # No transport endpoint is needed: stock rclpy's timer exercises the same
    # executor/context path, while facade teardown and error propagation remain real.
    monkeypatch.setattr(node, 'create_subscription', lambda *args, **kwargs: object())
    monkeypatch.setattr(_rclpy, 'WaitSet', racing_wait_set)
    monkeypatch.setattr(facade_process, 'wait_signal', wait_signal)
    monkeypatch.setattr(facade_process.sys, 'stdin', iter(['await_signal\n']))
    monkeypatch.setattr(facade_process, 'emit', lambda event, **values: events.append(event))
    try:
        facade_process.subscribe_commands(
            node, object, SimpleNamespace(topic='/controlled_signal', kind='Containers'), object())
        assert constructed.is_set()
        assert 'READY' in events and 'ERROR' not in events
    finally:
        node.destroy_node()
        if context.ok():
            context.shutdown()


@pytest.mark.parametrize('context_ok,signal_pending,error,expected', [
    (False, True, _rclpy.RCLError(INVALID_WAIT_SET), True),
    (True, True, _rclpy.RCLError(INVALID_WAIT_SET), False),
    (False, False, _rclpy.RCLError(INVALID_WAIT_SET), False),
    (False, True, _rclpy.RCLError('failed to initialize wait set: allocation failed'), False),
    (False, True, _rclpy.RCLError('failed to create guard condition: context invalid'), False),
    (False, True, RuntimeError(INVALID_WAIT_SET), False),
    (False, True, MemoryError('native receive adoption failed'), False),
    (False, False, ExternalShutdownException(), True),
    (True, True, ExternalShutdownException(), False),
])
def test_only_expected_shutdown_errors_are_normal(context_ok, signal_pending, error, expected):
    """A signal marker must not hide unrelated native, allocation or active-context errors."""
    context = SimpleNamespace(ok=lambda: context_ok)
    assert facade_process._is_expected_shutdown(error, context, signal_pending) is expected
