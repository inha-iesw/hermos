"""A fixed BufferCore/GpuTensor schema double for metadata algorithm tests.

This models owner contracts, not native storage or worker performance. Device
transport and the CPU binding suite independently check the real providers.
"""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest


ROOT = ('core', 'dtype_code', 'dtype_bits', 'dtype_lanes', 'shape', 'strides',
        'byte_offset', 'allocation_bytes')
CORE = ('vmm_socket_path', 'event_handle', 'publisher_pid', 'device_id', 'block_id',
        'uid', 'byte_size')


class Owner:
    def __init__(self):
        self.generation = 0
        self.values = {(0, 0): bytearray(), (0, 1): bytearray(64), (4,): [], (5,): []}
        self.events = []
        self.exports = 0
        self.filling = False
        self.fail_buffer = None

    @property
    def snapshot(self):
        return dict(generation=self.generation, exports=self.exports)

    def check(self, path, generation):
        if path and generation != self.generation:
            raise RuntimeError('stale structural generation')

    def info(self, path, generation):
        self.check(path, generation)
        self.events.append(('info', path))
        kind, members, size = 'scalar', (), 0
        if path in ((), (0,)):
            kind, members = 'message', ROOT if not path else CORE
        elif path == (0, 0):
            kind, size = 'string', len(self.values[path])
        elif path == (0, 1):
            kind, size = 'array', 64
        elif path in ((4,), (5,)):
            kind, size = 'sequence', len(self.values[path])
        return dict(kind=kind, members=members, size=size, generation=self.generation)

    def resize(self, path, generation, size):
        self.check(path, generation)
        if self.filling or self.exports:
            raise BufferError('resize after filling/export')
        self.values[path] = bytearray(size) if path == (0, 0) else [0] * size
        self.events.append(('resize', path, size))
        self.generation += 1

    def write(self, path, generation, value):
        self.check(path, generation)
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError('integer field requires int')
        value = int.__index__(value)
        if path[:1] in ((4,), (5,)):
            low, high = -(2**63), 2**63 - 1
        else:
            bits = (8 if path in ((1,), (2,)) or path[:2] in ((0, 0), (0, 1)) else
                    16 if path == (3,) else 32 if path in ((0, 2), (0, 3), (0, 4)) else 64)
            low, high = 0, 2**bits - 1
        if not low <= value <= high:
            raise ValueError('integer exceeds field range')
        self.filling = True
        self.events.append(('write', path, value))
        if path[:-1] in self.values:
            self.values[path[:-1]][path[-1]] = value
        else:
            self.values[path] = value

    def _write_on_worker(self, path, generation, value):
        self.events.append(('worker-write', path))
        self.write(path, generation, value)

    @contextmanager
    def buffer(self, path, generation):
        self.check(path, generation)
        self.filling = True
        self.exports += 1
        self.events.append(('buffer', path))
        view = memoryview(self.values[path])
        if self.fail_buffer == path:
            # Simulate assignment failure after acquisition.
            view = view.toreadonly()
        try:
            yield view
        finally:
            view.release()
            self.exports -= 1

    def read(self, path, generation):
        self.check(path, generation)
        self.events.append(('read', path))
        return (self.values[path[:-1]][path[-1]] if path[:-1] in self.values
                else self.values[path])

    def text_bytes(self, path, generation):
        self.check(path, generation)
        return bytes(self.values[path])


@pytest.fixture
def metadata_case():
    owner = Owner()
    descriptor = dict(core=dict(vmm_socket_path='/tmp/cuda-é.sock', event_handle=list(range(64)),
                                publisher_pid=123, device_id=0, block_id=2, uid=99, byte_size=4096),
                      dtype_code=1, dtype_bits=8, dtype_lanes=1, shape=[2, 3], strides=[3, 1],
                      byte_offset=0, allocation_bytes=4096)
    return SimpleNamespace(_owner=owner), descriptor
