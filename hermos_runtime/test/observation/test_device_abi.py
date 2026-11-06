"""Opt-in kernel ABI rejection checks without payload endpoints or borrows."""

import ctypes
import errno
import os
import uuid

import pytest


pytestmark = pytest.mark.skipif(os.environ.get('HERMOS_E3_RUN_DEVICE') != '1',
    reason='requires a fresh verified E3 kernel/wrapper device bundle')


class StatsArgs(ctypes.Structure):
    # v1 Linux 64-bit UAPI from hermos_kmod/hermos.h; the command encodes size.
    _fields_ = [
        ('topic_ptr', ctypes.c_void_p), ('topic_len', ctypes.c_uint64),
        ('buffer_addr', ctypes.c_uint64), ('capacity', ctypes.c_uint32),
        ('domain_id', ctypes.c_uint32), ('is_publisher', ctypes.c_uint8),
        ('reserved', ctypes.c_uint8 * 3), ('ret_count', ctypes.c_uint32),
        ('abi_version', ctypes.c_uint32), ('row_size', ctypes.c_uint32),
    ]


@pytest.mark.parametrize('args_size,version,row_size,capacity,expected', [
    (40, 1, 336, 1, errno.EPROTO),
    (48, 0, 336, 1, errno.EPROTO),
    (48, 2, 336, 1, errno.EPROTO),
    (48, 1, 335, 1, errno.EPROTO),
    (48, 1, 337, 1, errno.EPROTO),
    (48, 1, 336, 0, errno.EINVAL),
    (48, 1, 336, 1, 0),
])
def test_stats_abi_checks_precede_output_writes(args_size, version, row_size, capacity, expected):
    from hermos_runtime.environment import resolve_installation

    assert ctypes.sizeof(StatsArgs) == 48
    topic = ctypes.create_string_buffer(('/e3_abi_' + uuid.uuid4().hex).encode())
    # The output row and both surrounding canaries must stay untouched on
    # rejection, and for a successful query with zero matching endpoints.
    output = (ctypes.c_ubyte * (16 + 336 + 16))(*([0xa5] * 368))
    args = StatsArgs(
        topic_ptr=ctypes.addressof(topic), topic_len=len(topic.value),
        buffer_addr=ctypes.addressof(output) + 16, capacity=capacity,
        domain_id=211, ret_count=0xa5a5a5a5, abi_version=version, row_size=row_size)
    before = bytes(args)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
    libc.ioctl.restype = ctypes.c_int
    command = (3 << 30) | (args_size << 16) | (0xa6 << 8) | 34
    fd = os.open(str(resolve_installation().device), os.O_RDONLY | os.O_CLOEXEC)
    try:
        ctypes.set_errno(0)
        result = libc.ioctl(fd, command, ctypes.byref(args))
        actual_errno = ctypes.get_errno()
    finally:
        os.close(fd)
    assert bytes(output) == bytes([0xa5] * 368)
    if expected:
        assert (result, actual_errno) == (-1, expected)
        assert bytes(args) == before
    else:
        assert result == 0
        assert args.ret_count == 0
