"""Descriptor conversion and shared native-owner behavior without CUDA setup."""

from types import SimpleNamespace

import numpy as np
import pytest
from rclpy_hermos_gpu import runtime as runtime_module
from rclpy_hermos_gpu.tensor import lease as lease_module
from rclpy_hermos_gpu.tensor.loan import TensorLoan
from rclpy_hermos_gpu_msgs.msg import GpuTensor
from ros2_cuda_ipc_msgs.msg import BufferCore

from experiments.g5.gpu_latency import dds_endpoint as endpoint


def test_real_generated_message_round_trip_covers_every_field(descriptor):
    message = endpoint.to_ros_message(descriptor)
    assert isinstance(message, GpuTensor)
    assert isinstance(message.core, BufferCore)
    assert set(message.get_fields_and_field_types()) == set(descriptor)
    assert set(message.core.get_fields_and_field_types()) == set(descriptor['core'])
    assert message.core.vmm_socket_path == '/tmp/cuda_memory_pool_unit.sock'
    assert message.core.uid == 9876543210
    assert isinstance(message.core.event_handle, np.ndarray)
    decoded = endpoint.from_ros_message(message)
    assert decoded == descriptor
    assert all(type(item) is int for item in decoded['core']['event_handle'])
    assert all(type(item) is int for item in decoded['shape'] + decoded['strides'])


def test_generated_message_conversion_keeps_metadata_independent(descriptor):
    message = endpoint.to_ros_message(descriptor)
    descriptor['core']['event_handle'][0] = 255
    descriptor['shape'][0] = 99
    decoded = endpoint.from_ros_message(message)
    assert decoded['core']['event_handle'][0] == 0
    assert decoded['shape'] == [2, 3]


def test_borrow_reuses_tensor_loan_native_owner_and_stream_normalization(
        ros_node, dds_runtime, native_loan, monkeypatch):
    publisher = endpoint.DDSPublisher(ros_node, '/descriptor', object(), dds_runtime)
    monkeypatch.setattr(endpoint, 'torch_arguments', lambda *args: ((4,), 1, 8))
    stream = SimpleNamespace(cuda_stream=1)
    loan = publisher.borrow_tensor((4,), object(), 'cuda:0', stream)
    assert isinstance(loan, TensorLoan)
    assert loan._publisher is publisher and loan._handle is native_loan
    assert dds_runtime._native.borrow_arguments == [((4,), 1, 8, 0, publisher._owner_id)]
    assert dds_runtime._native.events == ['initialise', 'poll']


def test_prepare_failure_leaves_loan_cancellable(
        ros_node, dds_runtime, native_loan):
    publisher = endpoint.DDSPublisher(ros_node, '/descriptor', object(), dds_runtime)
    loan = TensorLoan(publisher, native_loan, object())
    native_loan.prepare_error = RuntimeError('prepare failed')
    with pytest.raises(RuntimeError, match='prepare failed'):
        with loan:
            publisher.publish(loan)
    assert native_loan.prepared == 1
    assert native_loan.cancelled == 1
    assert ros_node.published == []


@pytest.mark.parametrize('failure', ['conversion', 'publish'])
def test_post_prepare_failure_never_cancels_the_committed_generation(
        ros_node, dds_runtime, native_loan, monkeypatch, failure):
    publisher = endpoint.DDSPublisher(ros_node, '/descriptor', object(), dds_runtime)
    loan = TensorLoan(publisher, native_loan, object())
    if failure == 'conversion':
        def fail(_):
            raise ValueError('descriptor failed')
        monkeypatch.setattr(endpoint, 'to_ros_message', fail)
    else:
        ros_node.publish_error = ValueError('descriptor failed')
    with pytest.raises(ValueError, match='descriptor failed'):
        with loan:
            publisher.publish(loan)
    loan.cancel()
    assert loan._terminal is True
    assert native_loan.prepared == 1
    assert native_loan.cancelled == 0


def test_successful_publish_uses_real_descriptor_and_commits_once(
        descriptor, ros_node, dds_runtime, native_loan):
    publisher = endpoint.DDSPublisher(ros_node, '/descriptor', object(), dds_runtime)
    loan = TensorLoan(publisher, native_loan, object())
    with loan:
        publisher.publish(loan)
    assert endpoint.from_ros_message(ros_node.published[0]) == descriptor
    assert native_loan.cancelled == 0
    with pytest.raises(RuntimeError, match='terminal'):
        publisher.publish(loan)
    assert native_loan.prepared == 1


def test_foreign_loan_is_rejected_before_native_prepare(ros_node, dds_runtime, native_loan):
    publisher = endpoint.DDSPublisher(ros_node, '/descriptor', object(), dds_runtime)
    foreign = TensorLoan(object(), native_loan, object())
    for value in (foreign, object()):
        with pytest.raises(TypeError, match='another publisher'):
            publisher.publish(value)
    assert native_loan.prepared == 0


def test_subscription_defers_decoding_until_user_requests_descriptor(
        descriptor, ros_node, dds_runtime, monkeypatch):
    received = []
    subscription = endpoint.DDSSubscription(
        ros_node, '/descriptor', received.append, object(), dds_runtime)
    message = endpoint.to_ros_message(descriptor)
    original = endpoint.from_ros_message
    calls = []

    def decode(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(endpoint, 'from_ros_message', decode)
    ros_node.callback(message)
    assert calls == []
    assert isinstance(received[0], lease_module.TensorLease)
    assert received[0]._message is message
    assert received[0].descriptor == descriptor
    assert calls == [message]
    received[0].descriptor['shape'][0] = 99
    assert received[0].descriptor['shape'] == [2, 3]
    assert subscription.publisher_count == 3


def test_subscription_export_reuses_native_mapping_and_owner(
        descriptor, ros_node, dds_runtime, monkeypatch):
    received = []
    subscription = endpoint.DDSSubscription(
        ros_node, '/descriptor', received.append, object(), dds_runtime)
    ros_node.callback(endpoint.to_ros_message(descriptor))
    mapped = object()
    calls = []

    def native_map(value, stream_handle, owner):
        calls.append((value, stream_handle, owner))
        return mapped

    monkeypatch.setattr(lease_module, 'validate_torch_stream', lambda *args: 123)
    monkeypatch.setattr(lease_module, 'as_tensor', lambda acquire, **kwargs: acquire())
    dds_runtime._native.map = native_map
    assert received[0].as_tensor(stream=object()) is mapped
    assert calls == [(descriptor, 123, subscription._owner_id)]
    with pytest.raises(RuntimeError, match='only one Tensor export'):
        received[0].as_tensor(stream=object())


@pytest.mark.parametrize('action', ['stop', 'close'])
def test_retained_unexported_lease_cannot_map_after_endpoint_stops(
        descriptor, ros_node, dds_runtime, monkeypatch, action):
    received = []
    subscription = endpoint.DDSSubscription(
        ros_node, '/descriptor', received.append, object(), dds_runtime)
    ros_node.callback(endpoint.to_ros_message(descriptor))
    monkeypatch.setattr(lease_module, 'validate_torch_stream', lambda *args: 0)
    dds_runtime._native.map = lambda *args: pytest.fail('closed lease reached native map')
    subscription._stop_for_shutdown() if action == 'stop' else subscription.close()
    with pytest.raises(RuntimeError, match='closing|closed'):
        received[0].as_tensor(stream=object())


@pytest.mark.parametrize('kind', ['publisher', 'subscription'])
def test_close_waits_for_native_owners_and_then_is_idempotent(ros_node, dds_runtime, kind):
    value = (endpoint.DDSPublisher(ros_node, '/descriptor', object(), dds_runtime)
             if kind == 'publisher' else endpoint.DDSSubscription(
                 ros_node, '/descriptor', lambda _: None, object(), dds_runtime))
    dds_runtime._native.active[value._owner_id] = 1
    with pytest.raises(RuntimeError, match='active native owners'):
        value.close()
    assert value in dds_runtime._endpoints and ros_node.destroyed == []
    dds_runtime._native.active[value._owner_id] = 0
    value.close()
    value.close()
    assert value not in dds_runtime._endpoints
    assert len(ros_node.destroyed) == 1


@pytest.mark.parametrize('kind', ['publisher', 'subscription'])
def test_constructor_failure_removes_runtime_endpoint_registration(ros_node, dds_runtime, kind):
    ros_node.create_error = RuntimeError('ROS endpoint failed')
    with pytest.raises(RuntimeError, match='ROS endpoint failed'):
        if kind == 'publisher':
            endpoint.DDSPublisher(ros_node, '/descriptor', object(), dds_runtime)
        else:
            endpoint.DDSSubscription(ros_node, '/descriptor', lambda _: None, object(), dds_runtime)
    assert not dds_runtime._endpoints


def test_real_runtime_shutdown_retries_busy_endpoints_after_progress(ros_node, dds_runtime):
    publisher = endpoint.DDSPublisher(ros_node, '/descriptor', object(), dds_runtime)
    subscription = endpoint.DDSSubscription(
        ros_node, '/descriptor', lambda _: None, object(), dds_runtime)
    native = dds_runtime._native
    native.active.update({publisher._owner_id: 1, subscription._owner_id: 1})
    progress_calls = []

    def progress():
        assert dds_runtime._closing and native.closing
        assert publisher._closing and subscription._closing
        assert ros_node.destroyed == []
        assert 'drain' not in native.events
        progress_calls.append(True)
        native.active.clear()

    dds_runtime.shutdown(progress=progress)
    assert progress_calls == [True]
    assert dds_runtime._closed and not dds_runtime._endpoints
    assert publisher._closed and subscription._closed
    assert len(ros_node.destroyed) == 2
    assert native.events.index('seal') < native.events.index('drain')
    dds_runtime.shutdown(progress=lambda: pytest.fail('closed runtime progressed'))


def test_real_runtime_shutdown_timeout_can_retry_after_native_owner_released(
        ros_node, dds_runtime, monkeypatch):
    publisher = endpoint.DDSPublisher(ros_node, '/descriptor', object(), dds_runtime)
    native = dds_runtime._native
    native.active[publisher._owner_id] = 1
    with monkeypatch.context() as patch:
        ticks = iter((0.0, 1.0))
        patch.setattr(runtime_module.time, 'monotonic', lambda: next(ticks))
        with pytest.raises(RuntimeError, match='deadline'):
            dds_runtime.shutdown(timeout=0.1)
    assert not dds_runtime._closed and publisher in dds_runtime._endpoints
    assert ros_node.destroyed == [] and 'drain' not in native.events
    with pytest.raises(RuntimeError, match='closing'):
        dds_runtime._check_admission()
    native.active.clear()
    dds_runtime.shutdown()
    assert dds_runtime._closed and publisher._closed
    assert len(ros_node.destroyed) == 1
