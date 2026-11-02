"""Descriptor retention remains separate from acquiring a GPU read handle."""

from .export import as_tensor
from .layout import validate_torch_stream
from ..endpoint.metadata import read_descriptor


class TensorLease:
    """Retain a CPU descriptor; acquire a native read only when as_tensor is called.

    One export is allowed. Additional views come from the returned Tensor. The GPU
    mapping's actual access flags, rather than CPU heap protection, govern writes.
    Keep the bound stream valid through runtime drain and explicitly join other
    stream work before releasing the last Tensor alias.
    """

    def __init__(self, message, runtime, owner_id):
        self._message = message
        self._runtime = runtime
        self._owner_id = owner_id
        self._exported = False

    @property
    def descriptor(self):
        """Return a copy of small metadata without acquiring a GPU usage right."""
        return read_descriptor(self._message)

    def as_tensor(self, *, stream):
        """Map and export once on the explicit consumer stream; stale generations raise."""
        self._runtime._check_admission()
        if self._exported:
            raise RuntimeError('GPU lease allows only one Tensor export')
        handle = validate_torch_stream(stream, self._runtime.device)
        descriptor = self.descriptor
        self._exported = True
        return as_tensor(
            lambda: self._runtime._native.map(descriptor, handle, self._owner_id),
            device=self._runtime.device, stream=stream)
