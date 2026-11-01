"""Shape and stream normalization contracts before native allocation."""

import pytest

from rclpy_agnocast_gpu.tensor.layout import checked_shape, normalized_stream


@pytest.mark.parametrize('shape', [(True,), (-1,), (1 << 63,), (1 << 32, 1 << 32),
                                    (0, 1 << 32, 1 << 32), (1.5,)])
def test_invalid_shape_rejected_before_allocation(shape):
    with pytest.raises((TypeError, ValueError, OverflowError)):
        checked_shape(shape, itemsize=4, maximum=4096)


@pytest.mark.parametrize('shape,expected', [((), 4), ((0, 3), 0), ((2, 3), 24)])
def test_shape_uses_bytes_and_keeps_scalar_and_empty_layout(shape, expected):
    dimensions, size = checked_shape(shape, itemsize=4, maximum=4096)
    assert dimensions == shape
    assert size == expected


def test_shape_span_exceeding_runtime_limit_is_rejected():
    with pytest.raises(ValueError, match='limit'):
        checked_shape((1025,), itemsize=4, maximum=4096)


@pytest.mark.parametrize('stream', [0, 1])
def test_default_stream_tokens_normalize_to_legacy(stream):
    assert normalized_stream(stream) == 0


@pytest.mark.parametrize('stream', [2, -1, None, True])
def test_per_thread_or_invalid_stream_is_rejected(stream):
    with pytest.raises((TypeError, ValueError)):
        normalized_stream(stream)

