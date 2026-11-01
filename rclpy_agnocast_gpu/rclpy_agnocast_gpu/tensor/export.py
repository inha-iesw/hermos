"""Torch consumption with one explicit CUDA stream and one DLPack export."""

import importlib

from .layout import normalized_stream, validate_torch_stream


class StreamExport:
    """One-shot protocol owner that verifies Torch's actual requested stream."""

    def __init__(self, factory, device, stream):
        self._factory = factory
        self._device = device
        self._stream = stream
        self._capsule = None
        self._consumed = False

    def __dlpack_device__(self):
        return (2, self._device)

    def __dlpack__(self, stream=None, *, max_version=None, dl_device=None, copy=None):
        # The native exporter currently supplies the legacy managed-tensor ABI.
        # A consumer's maximum version does not require a newer producer ABI.
        del max_version
        if copy is True or (dl_device is not None and tuple(dl_device) != self.__dlpack_device__()):
            raise BufferError('GPU Tensor export does not copy or change devices')
        if self._consumed:
            raise BufferError('GPU lease allows only one DLPack export')
        if normalized_stream(stream) != self._stream:
            raise ValueError('DLPack requested a different CUDA stream')
        self._consumed = True
        native = self._factory()
        try:
            self._capsule = native.dlpack(stream)
            return self._capsule
        finally:
            # A saved exception traceback must not hide a mapped read owner if
            # capsule allocation fails before ownership reaches DLPack.
            native = None

    def discard_unconsumed(self):
        if self._capsule is not None:
            module = importlib.import_module('rclpy_agnocast_gpu._native')
            module.discard_dlpack(self._capsule)
            self._capsule = None


def as_tensor(factory, *, device, stream):
    """Consume DLPack in the declared Torch stream context, with no copy fallback."""
    import torch

    handle = validate_torch_stream(stream, device)
    exporter = StreamExport(factory, device, handle)
    try:
        with torch.cuda.device(device), torch.cuda.stream(stream):
            return torch.utils.dlpack.from_dlpack(exporter, copy=False)
    except BaseException:
        exporter.discard_unconsumed()
        raise
