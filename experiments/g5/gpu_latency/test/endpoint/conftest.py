"""Device-free lifecycle objects; descriptor schema remains the real ROS schema."""

from copy import deepcopy
import sys
from types import SimpleNamespace

import pytest
from rclpy_hermos_gpu import GpuRuntime
from rclpy_hermos_gpu import runtime as runtime_module


@pytest.fixture
def descriptor():
    return dict(core=dict(vmm_socket_path='/tmp/cuda_memory_pool_unit.sock',
                          event_handle=list(range(64)), publisher_pid=1234, device_id=0,
                          block_id=3, uid=9876543210, byte_size=4096),
                dtype_code=1, dtype_bits=8, dtype_lanes=1, shape=[2, 3], strides=[3, 1],
                byte_offset=0, allocation_bytes=4096)


class NativeLoan:
    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.prepared = self.cancelled = 0
        self.prepare_error = None

    def prepare(self):
        self.prepared += 1
        if self.prepare_error is not None:
            raise self.prepare_error
        return deepcopy(self.descriptor)

    def cancel(self):
        self.cancelled += 1


@pytest.fixture
def native_loan(descriptor):
    return NativeLoan(descriptor)


@pytest.fixture
def dds_runtime(native_loan, monkeypatch):
    class NativeRuntime:
        def __init__(self, *config):
            self.active = {}
            self.poisoned = self.closing = False
            self.events = []
            self.borrow_arguments = []

        def initialise(self):
            self.events.append('initialise')

        def borrow(self, *arguments):
            self.borrow_arguments.append(arguments)
            return native_loan

        def owner_active(self, owner):
            return self.active.get(owner, 0)

        def poll(self):
            self.events.append('poll')

        def seal(self):
            self.closing = True
            self.events.append('seal')

        def try_close(self):
            self.events.append('drain')
            return not any(self.active.values())

    monkeypatch.setattr(runtime_module, '_active_runtime', None)
    monkeypatch.setattr(runtime_module, '_epoch_started', False)
    monkeypatch.setitem(sys.modules, 'rclpy_hermos_gpu._native',
                        SimpleNamespace(Runtime=NativeRuntime))
    monkeypatch.delenv('G3_RECOVERY_MANIFEST', raising=False)
    monkeypatch.delenv('G3_RECOVERY_ACK', raising=False)
    runtime = GpuRuntime(block_count=2, block_bytes=4096, device=0,
                         max_reads=4, max_message_bytes=4096)
    runtime.initialise()
    runtime.register()
    return runtime


@pytest.fixture
def ros_node():
    class Node:
        def __init__(self):
            self.create_error = self.publish_error = None
            self.published = []
            self.destroyed = []
            self.publisher = SimpleNamespace(publish=self.publish)
            self.subscription = object()
            self.callback = None

        def create_publisher(self, message_type, topic, qos):
            if self.create_error is not None:
                raise self.create_error
            self.publisher_arguments = (message_type, topic, qos)
            return self.publisher

        def publish(self, message):
            if self.publish_error is not None:
                raise self.publish_error
            self.published.append(message)

        def create_subscription(self, message_type, topic, callback, qos):
            if self.create_error is not None:
                raise self.create_error
            self.subscription_arguments = (message_type, topic, qos)
            self.callback = callback
            return self.subscription

        def destroy_publisher(self, endpoint):
            assert endpoint is self.publisher
            self.destroyed.append(endpoint)
            return True

        def destroy_subscription(self, endpoint):
            assert endpoint is self.subscription
            self.destroyed.append(endpoint)
            return True

        def count_publishers(self, topic):
            assert topic == '/descriptor'
            return 3

    return Node()
