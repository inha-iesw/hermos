"""One explicitly initialized and registered GPU epoch per process."""

import importlib
import json
import math
import os
from pathlib import Path
from threading import RLock
import time


_active_runtime = None
_epoch_started = False
_process_lock = RLock()


def _integer(name, value, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f'{name} must be an integer')
    if value < (0 if zero else 1) or value > (1 << 63) - 1:
        raise ValueError(f'{name} is outside the supported range')
    return value


def current_runtime():
    """Return the initialized epoch before native endpoint creation, without lazy CUDA setup."""
    runtime = _active_runtime
    if runtime is None or not runtime._initialised:
        raise RuntimeError('GpuRuntime must be explicitly initialised first')
    if runtime._closing:
        raise RuntimeError('GPU runtime is closing')
    return runtime


class GpuRuntime:
    """Own a single process-wide producer pool and shared consumer mapper.

    Call initialise(), construct endpoints, then register() before spin or GPU work.
    ``block_count=0`` creates a consumer-only runtime. A registered epoch cannot be
    reinitialized; restart the whole supervised peer group instead. Streams passed
    to endpoints must stay valid until pending native owners have completed.
    """

    def __init__(self, *, block_count, block_bytes, device, max_reads, max_message_bytes):
        self._configuration = (
            _integer('block_count', block_count, zero=True),
            _integer('block_bytes', block_bytes),
            _integer('device', device, zero=True),
            _integer('max_reads', max_reads),
            _integer('max_message_bytes', max_message_bytes))
        self._initialised = False
        self._native = None
        self._registered = False
        self._registration_started = False
        self._closing = False
        self._closed = False
        self._endpoints = set()
        self._next_owner = 1
        self._lock = RLock()

    @property
    def block_count(self):
        """Number of producer slots fixed for this process epoch."""
        return self._configuration[0]

    @property
    def block_bytes(self):
        """Bytes reserved per producer slot, excluding core allocation granularity."""
        return self._configuration[1]

    @property
    def device(self):
        """CUDA device shared by all endpoints in this runtime."""
        return self._configuration[2]

    @property
    def max_reads(self):
        """Shared mapper admission limit fixed at initialization."""
        return self._configuration[3]

    @property
    def max_message_bytes(self):
        """Largest accepted GPU allocation metadata size for the shared mapper."""
        return self._configuration[4]

    def initialise(self):
        """Create CUDA resources once; endpoint factories never perform this implicitly."""
        global _active_runtime, _epoch_started
        with _process_lock, self._lock:
            if _epoch_started or self._native is not None or self._closing:
                raise RuntimeError('only one GPU epoch may be initialised per process')
            module = importlib.import_module('rclpy_hermos_gpu._native')
            native = module.Runtime(self.block_count, self.block_bytes, self.device,
                                    self.max_reads, self.max_message_bytes)
            # Latch before native side effects: failed partial initialization must
            # not be retried against a possibly retained core epoch.
            _epoch_started = True
            self._native = native
            native.initialise()
            self._initialised = True
            _active_runtime = self
        return self

    def register(self):
        """Publish the complete owned inventory and await the direct supervisor ACK once.

        Standalone diagnostics omit both registration variables. Supervised runs
        must use fresh absolute manifest/ACK paths supplied by IsolatedEpoch.
        """
        with self._lock:
            if not self._initialised:
                raise RuntimeError('GPU runtime must be initialised before registration')
            if self._registration_started:
                raise RuntimeError('GPU epoch was already registered or registration failed')
            if self._closing:
                raise RuntimeError('GPU runtime is closing')
            manifest = os.environ.get('G3_RECOVERY_MANIFEST')
            ack = os.environ.get('G3_RECOVERY_ACK')
            if bool(manifest) != bool(ack):
                raise RuntimeError('recovery manifest and ACK must be supplied together')
            if manifest is not None:
                manifest, ack = Path(manifest), Path(ack)
                temporary = Path(str(manifest) + '.tmp')
                if not manifest.is_absolute() or not ack.is_absolute():
                    raise RuntimeError('recovery registration paths must be absolute')
                if manifest.exists() or temporary.exists() or ack.exists():
                    raise RuntimeError('recovery registration must use a new epoch')
                timeout = float(os.environ.get('G3_RECOVERY_START_TIMEOUT', '10'))
                if not math.isfinite(timeout) or timeout <= 0:
                    raise ValueError('recovery startup timeout must be finite and positive')
            self._registration_started = True
            if manifest is not None:
                inventory = self._native.owned_ipc_paths()
                with temporary.open('x', encoding='utf-8') as output:
                    json.dump({'version': 1, 'pid': os.getpid(), 'resources': inventory}, output)
                    output.write('\n')
                temporary.replace(manifest)
                deadline = time.monotonic() + timeout
                while not ack.exists():
                    if time.monotonic() >= deadline:
                        raise RuntimeError('recovery registration ACK timed out')
                    time.sleep(0.005)
            self._registered = True

    def _check_admission(self):
        if not self._initialised:
            raise RuntimeError('GPU runtime is not initialised')
        if self._closing:
            raise RuntimeError('GPU runtime is closing')
        if not self._registered:
            raise RuntimeError('GPU runtime must register before GPU work or spin')
        self.poll()

    def _add_endpoint(self, endpoint):
        with self._lock:
            if self._closing:
                raise RuntimeError('GPU runtime is closing')
            owner = self._next_owner
            self._next_owner += 1
            self._endpoints.add(endpoint)
            return owner

    def _remove_endpoint(self, endpoint):
        with self._lock:
            self._endpoints.discard(endpoint)

    def poll(self):
        """Advance pending owners and propagate terminal poison even without callbacks."""
        if self._native is None:
            raise RuntimeError('GPU runtime is not initialised')
        self._native.poll()
        if self._native.poisoned:
            raise RuntimeError('GPU runtime poisoned; peer group restart required')

    @property
    def stats(self):
        """Return native wrapper counters; these are not physical VRAM measurements."""
        if self._native is None:
            raise RuntimeError('GPU runtime is not initialised')
        return self._native.stats

    @property
    def registered(self):
        """Whether this process completed its one registration handshake."""
        return self._registered

    def shutdown(self, timeout=3.0, *, progress=None):
        """Stop dispatch, seal/drain the mapper, then close the producer pool.

        The default deadline leaves two seconds before the supervisor's five-second
        TERM budget. Busy or poisoned shutdown raises; it never authorizes restart.
        CUDA streams/context must remain alive until this call succeeds. An optional
        nonblocking ``progress`` callback runs on busy iterations so already-started
        async callbacks can return owners. Its exceptions propagate. The caller must
        stop admission of new callbacks; futures depending on new timers/services
        are not guaranteed to finish during drain.
        """
        if progress is not None and not callable(progress):
            raise TypeError('GPU shutdown progress must be callable or None')
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('GPU shutdown timeout must be finite and positive')
        with self._lock:
            if self._closed:
                return
            self._closing = True
            endpoints = tuple(self._endpoints)
        for endpoint in endpoints:
            endpoint._stop_for_shutdown()
        if self._native is None:
            self._closed = True
            return
        self._native.seal()
        deadline = time.monotonic() + timeout
        last_error = None
        while True:
            self.poll()
            busy = False
            for endpoint in tuple(self._endpoints):
                try:
                    endpoint.close()
                except RuntimeError as error:
                    busy = True
                    last_error = error
            if not busy and self._native.try_close():
                self._closed = True
                return
            if time.monotonic() >= deadline:
                raise RuntimeError('GPU shutdown deadline expired; peer group restart required') from last_error
            if progress is not None:
                progress()
            time.sleep(0.001)
