"""Device-independent runtime fixtures shared by the GPU facade contract tests."""

import sys
from types import SimpleNamespace

import pytest

from rclpy_hermos_gpu import GpuRuntime
from rclpy_hermos_gpu import runtime as runtime_module


class NativeRuntime:
    def __init__(self, *config):
        self.config = config
        self.paths = ['/tmp/cuda_memory_pool_test.sock']
        self.poisoned = False
        self.closing = False
        self.stats = {}
        self.active = {}
        self.events = []
        self.close_ready = True

    def initialise(self):
        self.events.append('initialise')

    def owned_ipc_paths(self):
        return self.paths

    def poll(self):
        self.events.append('poll')

    def owner_active(self, owner):
        return self.active.get(owner, 0)

    def seal(self):
        self.closing = True
        self.events.append('seal')

    def try_close(self):
        self.events.append('drain')
        return self.close_ready


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr(runtime_module, '_active_runtime', None)
    monkeypatch.setattr(runtime_module, '_epoch_started', False)
    monkeypatch.setitem(sys.modules, 'rclpy_hermos_gpu._native',
                        SimpleNamespace(Runtime=NativeRuntime))
    monkeypatch.delenv('G3_RECOVERY_MANIFEST', raising=False)
    monkeypatch.delenv('G3_RECOVERY_ACK', raising=False)
    value = GpuRuntime(block_count=2, block_bytes=4096, device=0,
                       max_reads=4, max_message_bytes=4096)
    yield value


@pytest.fixture
def registered_runtime(runtime):
    """Provide an admitted epoch when initialization is only test setup."""
    runtime.initialise()
    runtime.register()
    return runtime
