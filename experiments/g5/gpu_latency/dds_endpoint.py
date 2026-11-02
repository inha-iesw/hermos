"""Benchmark-only DDS metadata transport over the production GPU loan/lease core.

These endpoints share the benchmark's single-threaded executor. They replace only
the descriptor transport; CUDA allocation, generation and DLPack ownership remain
the production runtime's responsibility.
"""

from rclpy_hermos_gpu.tensor.layout import normalized_stream, torch_arguments
from rclpy_hermos_gpu.tensor.lease import TensorLease
from rclpy_hermos_gpu.tensor.loan import TensorLoan
from rclpy_hermos_gpu_msgs.msg import GpuTensor
from ros2_cuda_ipc_msgs.msg import BufferCore


def to_ros_message(descriptor):
    """Copy native descriptor fields into an owning generated ROS message."""
    core = descriptor['core']
    return GpuTensor(
        core=BufferCore(
            vmm_socket_path=core['vmm_socket_path'],
            event_handle=[int(value) for value in core['event_handle']],
            publisher_pid=int(core['publisher_pid']), device_id=int(core['device_id']),
            block_id=int(core['block_id']), uid=int(core['uid']), byte_size=int(core['byte_size'])),
        dtype_code=int(descriptor['dtype_code']), dtype_bits=int(descriptor['dtype_bits']),
        dtype_lanes=int(descriptor['dtype_lanes']),
        shape=[int(value) for value in descriptor['shape']],
        strides=[int(value) for value in descriptor['strides']],
        byte_offset=int(descriptor['byte_offset']),
        allocation_bytes=int(descriptor['allocation_bytes']))


def from_ros_message(message):
    """Decode fresh native metadata; generated uint8 arrays contain NumPy scalars."""
    core = message.core
    return dict(
        core=dict(
            vmm_socket_path=core.vmm_socket_path,
            event_handle=[int(value) for value in core.event_handle],
            publisher_pid=int(core.publisher_pid), device_id=int(core.device_id),
            block_id=int(core.block_id), uid=int(core.uid), byte_size=int(core.byte_size)),
        dtype_code=int(message.dtype_code), dtype_bits=int(message.dtype_bits),
        dtype_lanes=int(message.dtype_lanes),
        shape=[int(value) for value in message.shape],
        strides=[int(value) for value in message.strides],
        byte_offset=int(message.byte_offset), allocation_bytes=int(message.allocation_bytes))


class DDSLease(TensorLease):
    """Retain owning ROS metadata and defer decoding until the normal Tensor export."""

    def __init__(self, message, runtime, owner_id, *, endpoint):
        super().__init__(message, runtime, owner_id)
        self._endpoint = endpoint

    @property
    def descriptor(self):
        """Reject late mapping after subscription shutdown, before entering native code."""
        if self._endpoint._closing or self._endpoint._closed:
            raise RuntimeError('DDS GPU subscription is closing or closed')
        return from_ros_message(self._message)


class DDSPublisher:
    """Publish existing TensorLoan generations through a stock DDS descriptor topic."""

    def __init__(self, node, topic, qos, runtime):
        self._node = node
        self._gpu_runtime = runtime
        self._closing = self._closed = False
        self._owner_id = runtime._add_endpoint(self)
        try:
            self._publisher = node.create_publisher(GpuTensor, topic, qos)
        except BaseException:
            runtime._remove_endpoint(self)
            raise

    def borrow_tensor(self, shape, dtype, device, stream):
        """Use the production allocation and stream validation path for the CUDA loan."""
        if self._closing:
            raise RuntimeError('DDS GPU publisher is closing')
        self._gpu_runtime._check_admission()
        dimensions, code, bits = torch_arguments(shape, dtype, device, stream, self._gpu_runtime)
        native = self._gpu_runtime._native.borrow(
            dimensions, code, bits, normalized_stream(stream.cuda_stream), self._owner_id)
        return TensorLoan(self, native, stream)

    def publish(self, loan):
        """Commit the GPU generation before converting and publishing its descriptor."""
        if not isinstance(loan, TensorLoan) or loan._publisher is not self:
            raise TypeError('GPU loan belongs to another publisher')
        if self._closing or loan._terminal:
            raise RuntimeError('DDS GPU publisher or loan is terminal')
        self._gpu_runtime._check_admission()
        descriptor = loan._handle.prepare()
        # A successful prepare commits this generation even if DDS later fails.
        loan._terminal = True
        self._publisher.publish(to_ros_message(descriptor))

    def _stop_for_shutdown(self):
        self._closing = True

    def close(self):
        """Stop admission; retry after all endpoint-owned native GPU work has drained."""
        if self._closed:
            return
        self._closing = True
        self._gpu_runtime.poll()
        if self._gpu_runtime._native.owner_active(self._owner_id):
            raise RuntimeError('DDS GPU publisher close: active native owners; release and retry')
        self._node.destroy_publisher(self._publisher)
        self._closed = True
        self._gpu_runtime._remove_endpoint(self)


class DDSSubscription:
    """Deliver the existing TensorLease API while retaining an owning DDS descriptor."""

    def __init__(self, node, topic, callback, qos, runtime):
        self._node = node
        self._topic = topic
        self._callback = callback
        self._gpu_runtime = runtime
        self._closing = self._closed = False
        self._owner_id = runtime._add_endpoint(self)
        try:
            self._subscription = node.create_subscription(GpuTensor, topic, self._receive, qos)
        except BaseException:
            runtime._remove_endpoint(self)
            raise

    def _receive(self, message):
        if self._closing:
            raise RuntimeError('DDS GPU subscription is closing')
        self._gpu_runtime._check_admission()
        self._callback(DDSLease(message, self._gpu_runtime, self._owner_id, endpoint=self))

    @property
    def publisher_count(self):
        """Return DDS discovery count for the descriptor topic."""
        return self._node.count_publishers(self._topic)

    def _stop_for_shutdown(self):
        self._closing = True

    def close(self):
        """Block late exports and retain registration until native owners drain."""
        if self._closed:
            return
        self._closing = True
        self._gpu_runtime.poll()
        if self._gpu_runtime._native.owner_active(self._owner_id):
            raise RuntimeError(
                'DDS GPU subscription close: active native owners; release and retry')
        self._node.destroy_subscription(self._subscription)
        self._closed = True
        self._gpu_runtime._remove_endpoint(self)
