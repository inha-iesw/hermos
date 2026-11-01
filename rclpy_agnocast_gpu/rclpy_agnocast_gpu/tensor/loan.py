"""Writable Tensor loan backed directly by a native producer reservation."""

from .export import as_tensor


class TensorLoan:
    """Keep the reservation until publish or safe cancellation after all writable aliases.

    Other streams must join their work into the bound stream before the final alias
    is released. Keeping a Torch view is sufficient to keep the native reservation.
    """

    def __init__(self, publisher, handle, stream):
        self._publisher = publisher
        self._handle = handle
        self._stream = stream
        self._terminal = False
        self._exported = False

    def as_tensor(self):
        """Export writable shared CUDA storage once; slice/detach retain its native owner."""
        if self._terminal or self._exported:
            raise RuntimeError('GPU loan is terminal or was already exported')
        self._publisher._gpu_runtime._check_admission()
        self._exported = True
        return as_tensor(lambda: self._handle, device=self._publisher._gpu_runtime.device,
                         stream=self._stream)

    def cancel(self):
        """Defer unpublished cancellation until writable aliases and GPU work finish."""
        if not self._terminal:
            self._handle.cancel()
            self._terminal = True

    @property
    def state(self):
        """Return the native BORROWED, PREPARED or CANCELLED state."""
        return self._handle.state

    @property
    def exports(self):
        """Number of active writable DLPack owners, including derived Torch storage aliases."""
        return self._handle.exports

    def __enter__(self):
        if self._terminal:
            raise RuntimeError('GPU loan is terminal')
        return self

    def __exit__(self, exception_type, exception, traceback):
        del exception, traceback
        try:
            self.cancel()
        except BaseException:
            if exception_type is None:
                raise
        return False
