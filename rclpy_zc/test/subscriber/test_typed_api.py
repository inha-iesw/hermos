"""Typed subscription validation and immutable Python proxy contracts."""

from dataclasses import FrozenInstanceError
from threading import RLock
from types import SimpleNamespace

import pytest
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup

from rclpy_agnocast.subscription.limits import SubscriptionLimits
from rclpy_agnocast.types.message import Message
from rclpy_agnocast.types.publisher import TypedPublisher
from subscriber.fakes import runtime_for_test


@pytest.mark.parametrize('field', [
    'max_messages', 'max_retained_bytes', 'max_message_bytes',
])
@pytest.mark.parametrize('value,error', [
    (0, ValueError), (-1, ValueError), (True, TypeError), (1.5, TypeError),
    (1 << 100, ValueError),
])
def test_limits_reject_invalid_values(field, value, error):
    arguments = dict(max_messages=2, max_retained_bytes=1024, max_message_bytes=512)
    arguments[field] = value
    with pytest.raises(error):
        SubscriptionLimits(**arguments)


def test_limits_cannot_be_mutated_after_validation():
    limits = SubscriptionLimits(2, 1024, 512)
    with pytest.raises(FrozenInstanceError):
        limits.max_messages = 0


class ReadOwner:
    """Independent native-owner protocol oracle; mutation must not reach it."""

    def info(self, path, generation):
        assert generation == 0
        if not path:
            return dict(kind='message', members=['value', 'values'], generation=0)
        if path == (1,):
            return dict(kind='sequence', size=2, generation=0)
        return dict(kind='scalar', generation=0)

    def read(self, path, generation):
        assert generation == 0
        return {(0,): 17, (1, 0): 23, (1, 1): 42}[path]


def test_readonly_root_and_nested_field_reject_writes_and_loan_context():
    message = Message(ReadOwner(), readonly=True)
    assert message.value == 17
    assert len(message.values) == 2
    assert message.values[1] == 42
    with pytest.raises(TypeError, match='read.only'):
        message.value = 2
    with pytest.raises(TypeError, match='read.only'):
        message.values[0] = 3
    with pytest.raises(TypeError, match='read.only'):
        message.values.resize(4)
    with pytest.raises(TypeError, match='read.only'):
        with message:
            pytest.fail('received roots are not producer loans')


def test_publisher_rejects_received_root_before_native_owner_access():
    publisher = TypedPublisher(SimpleNamespace(id=1), SimpleNamespace(_lifecycle_lock=RLock()))
    message = Message(ReadOwner(), readonly=True)
    for operation in (publisher.publish, publisher.cancel):
        with pytest.raises(TypeError, match='borrowed native root'):
            operation(message)


def make_runtime():
    node = SimpleNamespace(
        get_name=lambda: 'typed', context=SimpleNamespace(ok=lambda: True),
        add_waitable=lambda waitable: None, remove_waitable=lambda waitable: None,
        default_callback_group=MutuallyExclusiveCallbackGroup())
    return runtime_for_test(SimpleNamespace(close=lambda: None), node)


@pytest.mark.parametrize('change,error', [
    ({'topic': ''}, ValueError), ({'topic': 1}, TypeError),
    ({'callback': None}, TypeError), ({'depth': 0}, ValueError),
    ({'depth': True}, TypeError), ({'depth': 1 << 32}, ValueError),
    ({'limits': None}, TypeError), ({'callback_group': object()}, TypeError),
])
def test_invalid_arguments_do_not_resolve_or_create_endpoint(monkeypatch, change, error):
    from rclpy_agnocast import types

    monkeypatch.setattr(types, 'resolve_adapter', lambda value: pytest.fail('discovery called'))
    runtime = make_runtime()
    arguments = dict(message_type=object(), topic='/typed', callback=lambda message: None,
                     depth=2, limits=SubscriptionLimits(2, 1024, 512))
    arguments.update(change)
    with pytest.raises(error):
        runtime.create_typed_subscription(**arguments)
    assert runtime._subscriptions == set()
    runtime.close()


def test_async_generator_is_rejected_before_discovery(monkeypatch):
    from rclpy_agnocast import types

    async def callback(message):
        yield message

    monkeypatch.setattr(types, 'resolve_adapter', lambda value: pytest.fail('discovery called'))
    runtime = make_runtime()
    with pytest.raises(TypeError, match='async generator'):
        runtime.create_typed_subscription(
            object(), '/typed', callback, depth=1, limits=SubscriptionLimits(1, 64, 64))
    runtime.close()


def test_runtime_passes_all_limits_and_group_under_lifecycle_lock(monkeypatch):
    from rclpy_agnocast import types
    from rclpy_agnocast.subscription import typed

    runtime = make_runtime()
    message_type, handle, endpoint = object(), object(), object()
    calls = []
    group = MutuallyExclusiveCallbackGroup()

    def resolve(cls):
        assert cls is message_type
        return {'ros_type': 'test/msg/Typed'}

    def create(*args):
        assert runtime._lifecycle_lock._is_owned()
        calls.append(args)
        return handle

    async def callback(message):
        pass

    def subscription(native, function, *, runtime_owner, callback_group):
        assert native is handle
        assert function is callback
        assert runtime_owner is runtime
        assert callback_group is group
        return endpoint

    runtime._native_runtime.create_typed_subscription = create
    monkeypatch.setattr(types, 'resolve_adapter', resolve)
    monkeypatch.setattr(typed, 'TypedSubscription', subscription)
    try:
        result = runtime.create_typed_subscription(
            message_type, '/typed', callback, depth=3, limits=SubscriptionLimits(4, 2048, 512),
            callback_group=group)
        assert result is endpoint
        assert runtime._subscriptions == {endpoint}
        assert calls == [('test/msg/Typed', '/typed', 3, 4, 2048, 512)]
    finally:
        runtime._subscriptions.clear()
        runtime.close()


@pytest.mark.parametrize('kind', ['publisher', 'subscription'])
def test_gpu_factory_requires_explicit_runtime_before_creating_native_endpoint(monkeypatch, kind):
    import sys
    from rclpy_agnocast import types

    runtime = make_runtime()
    monkeypatch.setattr(types, 'resolve_adapter',
                        lambda _: {'ros_type': 'rclpy_agnocast_gpu_msgs/msg/GpuTensor'})

    def current():
        raise RuntimeError('GpuRuntime must be explicitly initialised first')

    monkeypatch.setitem(sys.modules, 'rclpy_agnocast_gpu.runtime',
                        SimpleNamespace(current_runtime=current))
    monkeypatch.setitem(sys.modules, 'rclpy_agnocast_gpu.endpoint.publisher',
                        SimpleNamespace(TensorPublisher=object))
    monkeypatch.setitem(sys.modules, 'rclpy_agnocast_gpu.endpoint.subscription',
                        SimpleNamespace(TensorSubscription=object))
    runtime._native_runtime.create_typed_publisher = lambda *args: pytest.fail('created publisher')
    runtime._native_runtime.create_typed_subscription = lambda *args: pytest.fail('created subscriber')
    with pytest.raises(RuntimeError, match='explicitly initialised'):
        if kind == 'publisher':
            runtime.create_typed_publisher(object(), '/gpu', depth=1, max_storage_bytes=4096)
        else:
            runtime.create_typed_subscription(object(), '/gpu', lambda _: None, depth=1,
                                               limits=SubscriptionLimits(1, 4096, 4096))
    assert runtime._subscriptions == set()
    runtime.close()
