"""Tensor publication reuses the typed metadata worker after GPU preparation."""

from rclpy_hermos.types.publisher import TypedPublisher

from .metadata import fill_descriptor
from ..tensor.layout import torch_arguments, normalized_stream
from ..tensor.loan import TensorLoan


class TensorPublisher:
    """Publish GPU loans without retaining a borrowed CPU descriptor during computation."""

    def __init__(self, handle, runtime_owner, *, gpu_runtime):
        self._runtime_owner = runtime_owner
        self._gpu_runtime = gpu_runtime
        self._typed = TypedPublisher(handle, runtime_owner)
        self._closing = False
        self._closed = False
        self._owner_id = gpu_runtime._add_endpoint(self)

    def borrow_tensor(self, shape, dtype, device, stream):
        """Borrow dense offset-zero CUDA storage with an explicit dtype, device and stream."""
        with self._runtime_owner._lifecycle_lock:
            if self._closing:
                raise RuntimeError('GPU publisher is closing')
            self._gpu_runtime._check_admission()
            dimensions, code, bits = torch_arguments(shape, dtype, device, stream, self._gpu_runtime)
            native = self._gpu_runtime._native.borrow(
                dimensions, code, bits, normalized_stream(stream.cuda_stream), self._owner_id)
            return TensorLoan(self, native, stream)

    def publish(self, loan):
        """Prepare after writable aliases end, then borrow/fill/publish descriptor metadata."""
        with self._runtime_owner._lifecycle_lock:
            if not isinstance(loan, TensorLoan) or loan._publisher is not self:
                raise TypeError('GPU loan belongs to another publisher')
            if self._closing or loan._terminal:
                raise RuntimeError('GPU publisher or loan is terminal')
            self._gpu_runtime._check_admission()
            descriptor = loan._handle.prepare()
            # Successful prepare commits the core publication. Descriptor failure must
            # not cancel or recycle that generation early.
            loan._terminal = True
            with self._typed.borrow_message() as message:
                fill_descriptor(message, descriptor)
                self._typed.publish(message)

    def cancel(self, loan):
        """Safely cancel an unpublished loan owned by this endpoint."""
        if not isinstance(loan, TensorLoan) or loan._publisher is not self:
            raise TypeError('GPU loan belongs to another publisher')
        loan.cancel()

    @property
    def max_storage_bytes(self):
        """CPU descriptor capacity limit, distinct from GPU pool and mapping limits."""
        return self._typed.max_storage_bytes

    def _stop_for_shutdown(self):
        with self._runtime_owner._lifecycle_lock:
            self._closing = True

    def close(self):
        """Stop borrows and fail retryably while this endpoint owns aliases or GPU work."""
        with self._runtime_owner._lifecycle_lock:
            if self._closed:
                return
            self._closing = True
            self._gpu_runtime.poll()
            if self._gpu_runtime._native.owner_active(self._owner_id):
                raise RuntimeError('GPU publisher close: active native owners; release and retry')
            self._typed.close()
            self._closed = True
            self._gpu_runtime._remove_endpoint(self)
            self._runtime_owner._gpu_publishers.discard(self)
