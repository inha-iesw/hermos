"""Real rclpy continuation progress while GPU admission and new callbacks are sealed."""

from threading import Event, RLock, Thread
import time
from types import SimpleNamespace

import pytest
import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.context import Context
from rclpy.executors import SingleThreadedExecutor
from rclpy.signals import SignalHandlerOptions
from rclpy.task import Future
from std_msgs.msg import String
from std_srvs.srv import Trigger

from rclpy_agnocast import _native
from rclpy_agnocast_gpu import GpuRuntime, launch
from rclpy_agnocast_gpu.endpoint.subscription import TensorSubscription


class NativeEpoch:
    poisoned = False
    closing = False

    def __init__(self):
        self.sealed = Event()

    def poll(self):
        pass

    def seal(self):
        self.closing = True
        self.sealed.set()

    def owner_active(self, owner):
        return 0

    def try_close(self):
        return True


def spin_until(executor, predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, 'rclpy did not reach the expected callback state'
        executor.spin_once(timeout_sec=0.01)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv('ROS_LOG_DIR', str(tmp_path / 'ros-log'))
    context = Context()
    rclpy.init(args=[], context=context, signal_handler_options=SignalHandlerOptions.NO)
    node = rclpy.create_node('gpu_async_shutdown', context=context)
    executor = launch._draining_executor(context=context)
    executor.add_node(node)
    gpu = GpuRuntime(block_count=0, block_bytes=64, device=0, max_reads=2, max_message_bytes=64)
    # CUDA itself is replaced; the descriptor owner, Waitable and coroutine task
    # below are the real native adapter and rclpy implementation.
    gpu._native = NativeEpoch()
    gpu._initialised = gpu._registered = True
    owner = SimpleNamespace(_rclpy_node=node, _subscriptions=set(), _lifecycle_lock=RLock())
    futures = []

    def future():
        result = Future()
        futures.append(result)
        return result

    def subscribe(callback):
        handle = _native._TestTypedSubscription(4, 16384, 4096)
        subscription = TensorSubscription(handle, callback, runtime_owner=owner,
                                           callback_group=ReentrantCallbackGroup(), gpu_runtime=gpu)
        owner._subscriptions.add(subscription)
        return subscription, handle

    yield SimpleNamespace(context=context, node=node, executor=executor, runtime=gpu,
                          future=future, subscribe=subscribe)
    for value in futures:
        if not value.done():
            value.set_result(None)
    executor.begin_drain()
    gpu.shutdown(timeout=1, progress=executor.progress)
    assert executor.shutdown(timeout_sec=1)
    assert not owner._subscriptions
    node.destroy_node()
    context.shutdown()


def test_suspended_callback_returns_native_owner_after_external_future_completes(harness):
    gate = harness.future()
    entered, resumed = Event(), Event()

    async def callback(lease):
        entered.set()
        await gate
        # Keep the real descriptor alive through the continuation.
        assert lease._message.signed == 17
        resumed.set()

    subscription, handle = harness.subscribe(callback)
    assert handle.push(17)
    spin_until(harness.executor, entered.is_set)
    assert subscription._dispatching == handle.stats.outstanding_leases == 1

    def complete():
        assert harness.runtime._native.sealed.wait(3)
        gate.set_result(None)

    completer = Thread(target=complete)
    completer.start()
    try:
        harness.executor.begin_drain()
        harness.runtime.shutdown(timeout=1, progress=harness.executor.progress)
    finally:
        completer.join(3)
    assert not completer.is_alive()
    assert resumed.is_set()
    assert subscription._dispatching == handle.stats.outstanding_leases == 0
    assert handle.release_count == 1


def test_never_ready_future_reaches_deadline_without_invalidating_its_owner(harness):
    gate = harness.future()
    entered = Event()

    async def callback(lease):
        entered.set()
        await gate

    subscription, handle = harness.subscribe(callback)
    assert handle.push(1)
    spin_until(harness.executor, entered.is_set)
    harness.executor.begin_drain()
    with pytest.raises(RuntimeError, match='deadline'):
        harness.runtime.shutdown(timeout=0.03, progress=harness.executor.progress)
    assert subscription._dispatching == handle.stats.outstanding_leases == 1
    assert not harness.runtime._closed
    # Fixture completes the same Future and retries with the still-owned state.


def test_completed_continuation_exception_is_propagated_after_native_owner_release(harness):
    gate = harness.future()
    entered = Event()
    failure = LookupError('async continuation failure')

    async def callback(lease):
        entered.set()
        await gate
        raise failure

    subscription, handle = harness.subscribe(callback)
    assert handle.push(1)
    spin_until(harness.executor, entered.is_set)
    gate.set_result(None)
    harness.executor.begin_drain()
    with pytest.raises(LookupError) as error:
        harness.runtime.shutdown(timeout=1, progress=harness.executor.progress)
    assert error.value is failure
    assert subscription._dispatching == handle.stats.outstanding_leases == 0


def test_drain_discards_cached_ready_entities_and_does_not_start_new_callbacks(harness):
    calls = []
    group = ReentrantCallbackGroup()
    harness.node.create_timer(0.001, lambda: calls.append('first'), callback_group=group)
    harness.node.create_timer(0.001, lambda: calls.append('cached-timer'), callback_group=group)
    harness.node.create_subscription(String, '/g4_async_gate', lambda _: calls.append('dds'), 2,
                                     callback_group=group)
    publisher = harness.node.create_publisher(String, '/g4_async_gate', 2)
    subscription, handle = harness.subscribe(lambda _: calls.append('gpu'))
    time.sleep(0.005)
    # Leave the normal executor's readiness generator paused after its first timer.
    harness.executor.spin_once(timeout_sec=0)
    assert calls == ['first']
    harness.executor.begin_drain()
    assert handle.push(3)
    publisher.publish(String(data='pending during drain'))
    for _ in range(8):
        harness.executor.progress()
        time.sleep(0.002)
    assert calls == ['first']
    assert handle.stats.queued_leases == handle.stats.outstanding_leases == 1
    assert subscription._dispatching == 0


def test_client_reply_can_resume_existing_callback_during_drain(harness):
    server = rclpy.create_node('gpu_async_reply_server', context=harness.context)
    server_executor = SingleThreadedExecutor(context=harness.context)
    server_executor.add_node(server)
    completed, entered = Event(), Event()
    response_seen = []

    def respond(request, response):
        response.success = True
        return response

    server.create_service(Trigger, '/g4_async_reply', respond)
    client = harness.node.create_client(Trigger, '/g4_async_reply')
    assert client.wait_for_service(timeout_sec=3)
    response = client.call_async(Trigger.Request())

    async def callback(lease):
        entered.set()
        reply = await response
        response_seen.append(reply.success)

    subscription, handle = harness.subscribe(callback)
    assert handle.push(1)
    spin_until(harness.executor, entered.is_set)

    def run_server():
        assert harness.runtime._native.sealed.wait(3)
        while not completed.is_set():
            server_executor.spin_once(timeout_sec=0.01)

    thread = Thread(target=run_server)
    thread.start()
    try:
        harness.executor.begin_drain()
        harness.runtime.shutdown(timeout=1, progress=harness.executor.progress)
    finally:
        completed.set()
        thread.join(3)
        assert server_executor.shutdown(timeout_sec=1)
        server.destroy_node()
    assert not thread.is_alive()
    assert response_seen == [True]
    assert subscription._dispatching == handle.stats.outstanding_leases == 0
