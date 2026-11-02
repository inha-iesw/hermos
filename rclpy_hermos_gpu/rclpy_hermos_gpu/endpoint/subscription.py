"""Use the existing Waitable dispatcher with GPU descriptor-backed leases."""

from rclpy_hermos.subscription.typed import TypedSubscription

from ..tensor.lease import TensorLease


class TensorSubscription(TypedSubscription):
    """Retain CPU descriptor budgets and GPU aliases independently through close."""

    def __init__(self, handle, callback, *, runtime_owner, callback_group, gpu_runtime):
        self._gpu_runtime = gpu_runtime
        self._owner_id = gpu_runtime._add_endpoint(self)
        try:
            super().__init__(handle, callback, runtime_owner=runtime_owner,
                             callback_group=callback_group)
        except BaseException:
            gpu_runtime._remove_endpoint(self)
            raise

    def _make_message(self, owner):
        self._gpu_runtime._check_admission()
        return TensorLease(super()._make_message(owner), self._gpu_runtime, self._owner_id)

    def _check_idle(self):
        super()._check_idle()
        self._gpu_runtime.poll()
        if self._gpu_runtime._native.owner_active(self._owner_id):
            raise RuntimeError('GPU subscription close: active native owners; release and retry')

    def _finish_close(self):
        super()._finish_close()
        self._gpu_runtime._remove_endpoint(self)
