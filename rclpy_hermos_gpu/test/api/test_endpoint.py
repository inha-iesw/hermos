"""Tensor publication, deferred mapping and descriptor ownership contracts."""

from types import SimpleNamespace

import pytest

from rclpy_hermos_gpu.endpoint.publisher import TensorPublisher
from rclpy_hermos_gpu.tensor.loan import TensorLoan


def test_prepared_generation_is_not_cancelled_when_descriptor_borrow_fails(registered_runtime):
    events = []

    def borrow():
        events.append('metadata-borrow')
        raise RuntimeError('metadata borrow failed')

    publisher = TensorPublisher(
        SimpleNamespace(id=1, borrow_message=borrow, close=lambda: None),
        SimpleNamespace(_lifecycle_lock=registered_runtime._lock, _gpu_publishers=set()), gpu_runtime=registered_runtime)
    native = SimpleNamespace(prepare=lambda: events.append('prepare') or {},
                             cancel=lambda: events.append('cancel'))
    with TensorLoan(publisher, native, None) as loan:
        with pytest.raises(RuntimeError, match='metadata borrow failed'):
            publisher.publish(loan)
        with pytest.raises(RuntimeError, match='terminal'):
            publisher.publish(loan)
    assert events == ['prepare', 'metadata-borrow']


def test_live_writable_alias_rejection_keeps_loan_retryable(registered_runtime):

    def prepare():
        raise BufferError('writable aliases remain')

    publisher = TensorPublisher(SimpleNamespace(id=1),
                                SimpleNamespace(_lifecycle_lock=registered_runtime._lock, _gpu_publishers=set()),
                                gpu_runtime=registered_runtime)
    native = SimpleNamespace(prepare=prepare, cancel=lambda: None)
    loan = TensorLoan(publisher, native, None)
    with pytest.raises(BufferError, match='aliases'):
        publisher.publish(loan)
    assert not loan._terminal
    loan.cancel()


def test_endpoint_close_preserves_shared_mapper_until_runtime_shutdown(registered_runtime):
    closed = []
    publisher = TensorPublisher(SimpleNamespace(id=1, close=lambda: closed.append(True)),
                                SimpleNamespace(_lifecycle_lock=registered_runtime._lock, _gpu_publishers=set()),
                                gpu_runtime=registered_runtime)
    registered_runtime._native.active[publisher._owner_id] = 1
    with pytest.raises(RuntimeError, match='active native owners'):
        publisher.close()
    assert closed == []
    assert not registered_runtime._native.closing
    registered_runtime._native.active.clear()
    publisher.close()
    assert closed == [True]
    assert publisher not in registered_runtime._endpoints
    assert not registered_runtime._native.closing


def test_descriptor_retention_does_not_map_until_first_tensor_export(registered_runtime, monkeypatch):
    from rclpy_hermos_gpu.tensor import lease as lease_module

    calls = []
    descriptor = {'core': {'uid': 123}}
    monkeypatch.setattr(lease_module, 'read_descriptor', lambda _: descriptor)
    monkeypatch.setattr(lease_module, 'validate_torch_stream', lambda *_: 77)
    monkeypatch.setattr(lease_module, 'as_tensor', lambda factory, **_: factory())
    registered_runtime._native.map = lambda *args: calls.append(args) or 'native tensor'
    lease = lease_module.TensorLease(object(), registered_runtime, 3)
    assert lease.descriptor == descriptor
    assert calls == []
    assert lease.as_tensor(stream=object()) == 'native tensor'
    assert calls == [(descriptor, 77, 3)]
    with pytest.raises(RuntimeError, match='one Tensor'):
        lease.as_tensor(stream=object())
    assert len(calls) == 1


def test_descriptor_copy_releases_cpu_owner_without_waiting_for_cyclic_gc():
    import gc
    import weakref
    from rclpy_hermos_gpu.endpoint.metadata import read_descriptor

    destroyed = []

    class Owner:
        def info(self, path, generation):
            assert generation == 0
            if not path:
                return dict(kind='message', members=('shape', 'core'), generation=0)
            if path == (0,):
                return dict(kind='sequence', size=2, generation=0)
            if path == (1,):
                return dict(kind='message', members=('uid', 'vmm_socket_path'), generation=0)
            return dict(kind='string' if path == (1, 1) else 'scalar', generation=0)

        def read(self, path, generation):
            return {(0, 0): 2, (0, 1): 3, (1, 0): 7}[path]

        def text_bytes(self, path, generation):
            return b'/tmp/gpu.sock'

        def __del__(self):
            destroyed.append(True)

    enabled = gc.isenabled()
    gc.disable()
    try:
        owner = Owner()
        observed = weakref.ref(owner)
        message = SimpleNamespace(_owner=owner)
        result = read_descriptor(message)
        del message, owner
        assert result == {'shape': [2, 3], 'core': {'uid': 7, 'vmm_socket_path': '/tmp/gpu.sock'}}
        assert observed() is None
        assert destroyed == [True]
    finally:
        if enabled:
            gc.enable()

