"""Validate the offset-zero dense facade before CUDA or native allocation."""

import operator
import sys


def checked_shape(shape, *, itemsize, maximum):
    """Return int64 dimensions and byte span, rejecting even empty stride overflow."""
    dimensions = []
    volume = 1
    empty = False
    for dimension in shape:
        if isinstance(dimension, bool):
            raise TypeError('Tensor shape dimensions must be integers')
        dimension = operator.index(dimension)
        if dimension < 0 or dimension > sys.maxsize:
            raise ValueError('Tensor shape dimensions must fit non-negative int64')
        factor = max(dimension, 1)
        if volume > sys.maxsize // factor:
            raise ValueError('Tensor shape stride volume overflows int64')
        volume *= factor
        empty |= dimension == 0
        dimensions.append(dimension)
    size = 0 if empty else volume * itemsize
    if size > maximum:
        raise ValueError('Tensor byte span exceeds runtime limit')
    return tuple(dimensions), size


def normalized_stream(stream):
    """Translate DLPack legacy sentinel 1 to CUDA 0; per-thread stream 2 is unsupported."""
    if isinstance(stream, bool) or not isinstance(stream, int):
        raise TypeError('CUDA stream must be an integer handle')
    if stream < 0 or stream == 2:
        raise ValueError('per-thread or negative CUDA stream is unsupported')
    return 0 if stream == 1 else stream


def torch_arguments(shape, dtype, device, stream, runtime):
    """Validate Torch dtype/device/stream without silently changing requested storage."""
    import torch

    names = {
        'torch.int8': (0, 8), 'torch.int16': (0, 16),
        'torch.int32': (0, 32), 'torch.int64': (0, 64),
        'torch.uint8': (1, 8), 'torch.uint16': (1, 16),
        'torch.uint32': (1, 32), 'torch.uint64': (1, 64),
        'torch.float16': (2, 16), 'torch.float32': (2, 32),
        'torch.float64': (2, 64),
    }
    if not isinstance(dtype, torch.dtype) or str(dtype) not in names:
        raise TypeError('unsupported CUDA Tensor dtype')
    code, bits = names[str(dtype)]
    dimensions, _ = checked_shape(shape, itemsize=bits // 8,
                                  maximum=min(runtime.block_bytes, runtime.max_message_bytes))
    selected = torch.device(device)
    if selected.type != 'cuda' or selected.index != runtime.device:
        raise ValueError('device must explicitly identify the runtime CUDA device')
    validate_torch_stream(stream, runtime.device)
    return dimensions, code, bits


def validate_torch_stream(stream, device):
    """Require a live caller-owned Torch stream on the runtime device."""
    import torch

    if not isinstance(stream, torch.cuda.Stream):
        raise TypeError('stream must be a torch.cuda.Stream')
    if stream.device.type != 'cuda' or stream.device.index != device:
        raise ValueError('stream device does not match GPU runtime')
    return normalized_stream(stream.cuda_stream)
