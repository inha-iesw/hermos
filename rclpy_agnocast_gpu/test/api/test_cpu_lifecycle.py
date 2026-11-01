"""GPU owners keep the CPU runtime and Node alive until explicit close succeeds."""

from types import SimpleNamespace

import pytest


@pytest.fixture
def cpu_gpu_publishers(registered_runtime, monkeypatch):
    from rclpy_agnocast import runtime as cpu_module
    from rclpy_agnocast import types

    events = []
    handles = []

    def create_handle(*arguments):
        index = len(handles) + 1
        handle = SimpleNamespace(id=index, close=lambda: events.append(('publisher-close', index)))
        handles.append(handle)
        return handle

    native = SimpleNamespace(create_typed_publisher=create_handle,
                             prepare_close=lambda: events.append('cpu-native-prepare'),
                             close=lambda: events.append('cpu-runtime-close'))
    monkeypatch.setattr(cpu_module, '_active_runtime', None)
    monkeypatch.setattr(cpu_module, '_preflight', lambda: None)
    monkeypatch.setattr(cpu_module._native, 'RuntimeCore', SimpleNamespace(create=lambda _: native))
    monkeypatch.setattr(types, 'resolve_adapter',
                        lambda _: {'ros_type': 'rclpy_agnocast_gpu_msgs/msg/GpuTensor'})
    node = SimpleNamespace(get_name=lambda: 'gpu_close', context=SimpleNamespace(ok=lambda: True))
    cpu = cpu_module.AgnocastRuntime(node)
    publishers = [cpu.create_typed_publisher(object(), f'/gpu_{index}', depth=1,
                                             max_storage_bytes=4096) for index in range(2)]
    return cpu, publishers, events


@pytest.mark.parametrize('target', ['runtime', 'node'])
@pytest.mark.parametrize('with_subscription', [False, True])
def test_runtime_or_node_close_rejects_retained_gpu_publisher_owners(
        runtime, cpu_gpu_publishers, monkeypatch, target, with_subscription):
    from rclpy_agnocast.compat.node import Node, RclpyNode

    cpu, publishers, events = cpu_gpu_publishers
    if with_subscription:
        class Subscription:
            def _mark_closing(self):
                events.append('subscription-stop')

            def _join_and_drain(self):
                events.append('subscription-drain')

            def _check_idle(self):
                pass

            def _remove_guard(self):
                pass

            def _finish_close(self):
                cpu._subscriptions.discard(self)

        cpu._subscriptions.add(Subscription())
    runtime._native.active[publishers[0]._owner_id] = 1
    if target == 'runtime':
        close = cpu.close
    else:
        node = Node.__new__(Node)
        node._agnocast_lock = cpu._lifecycle_lock
        node._agnocast_runtime = cpu
        node._agnocast_publishers = set(publishers)
        node._agnocast_subscriptions = set()
        node._agnocast_closing = False
        node._agnocast_destroyed = False
        monkeypatch.setattr(RclpyNode, 'destroy_node', lambda _: events.append('base-node-close'))
        close = node.destroy_node
    with pytest.raises(RuntimeError, match='active native owners'):
        close()
    assert not cpu._closed
    assert 'cpu-runtime-close' not in events
    assert 'cpu-native-prepare' not in events
    if with_subscription:
        assert 'subscription-stop' in events
    assert all(publisher._closing for publisher in publishers)
    with pytest.raises(RuntimeError, match='closing'):
        cpu.create_typed_publisher(object(), '/after_close', depth=1, max_storage_bytes=4096)
    with pytest.raises(RuntimeError, match='closing'):
        cpu.create_publisher('/after_close', depth=1, max_payload_bytes=64)
    assert not runtime._native.closing
    if target == 'node':
        assert not node._agnocast_destroyed
        assert 'base-node-close' not in events
    runtime._native.active.clear()
    close()
    assert cpu._closed
    assert not cpu._gpu_publishers
    assert all(publisher._closed for publisher in publishers)
    assert events.index(('publisher-close', 1)) < events.index('cpu-runtime-close')
    assert events.index(('publisher-close', 2)) < events.index('cpu-runtime-close')
    assert not runtime._native.closing
    if target == 'node':
        assert node._agnocast_destroyed
        assert events.index('cpu-runtime-close') < events.index('base-node-close')


def test_individual_gpu_publisher_close_removes_cpu_runtime_registration(cpu_gpu_publishers):
    cpu, publishers, _ = cpu_gpu_publishers
    assert cpu._gpu_publishers == set(publishers)
    publishers[0].close()
    assert cpu._gpu_publishers == {publishers[1]}
    cpu.close()
    assert not cpu._gpu_publishers


def test_close_cannot_pass_owner_check_while_an_admitted_borrow_is_still_validating(
        runtime, cpu_gpu_publishers, monkeypatch):
    from threading import Event, Thread
    from rclpy_agnocast_gpu.endpoint import publisher as publisher_module

    cpu, publishers, events = cpu_gpu_publishers
    publisher = publishers[0]
    validating, proceed, closing, close_finished = (Event() for _ in range(4))
    borrowed, close_errors = [], []

    def arguments(*args):
        validating.set()
        assert proceed.wait(3)
        return (1,), 1, 8

    def borrow(*args):
        runtime._native.active[publisher._owner_id] = 1
        return SimpleNamespace(cancel=lambda: runtime._native.active.clear())

    def close():
        closing.set()
        try:
            publisher.close()
        except RuntimeError as error:
            close_errors.append(error)
        finally:
            close_finished.set()

    monkeypatch.setattr(publisher_module, 'torch_arguments', arguments)
    runtime._native.borrow = borrow
    borrower = Thread(target=lambda: borrowed.append(publisher.borrow_tensor(
        (1,), object(), 'cuda:0', SimpleNamespace(cuda_stream=77))))
    closer = Thread(target=close)
    borrower.start()
    try:
        assert validating.wait(3)
        closer.start()
        assert closing.wait(3)
        assert not close_finished.wait(0.05)
    finally:
        proceed.set()
        borrower.join(3)
        if closer.ident is not None:
            closer.join(3)
    assert not borrower.is_alive() and not closer.is_alive()
    assert len(borrowed) == len(close_errors) == 1
    assert 'active native owners' in str(close_errors[0])
    assert ('publisher-close', 1) not in events
    borrowed[0].cancel()
    cpu.close()

