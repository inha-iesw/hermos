"""Direct-peer inventory and ACK protocol, without native or GPU imports."""

import json
import math
import os
from pathlib import Path
import time


class Registration:
    """One inventory publication followed by the owning supervisor's ACK.

    Construct with :meth:`from_environment` before changing caller admission
    state. Call :meth:`wait` only after endpoints and all owned IPC resources
    exist, and before dispatch. A failed publication or wait cannot be retried
    with the same object; the process owner decides how to terminate the epoch.
    """

    def __init__(self, manifest, ack, timeout):
        self.manifest = manifest
        self.ack = ack
        self.timeout = timeout
        self._started = False

    @classmethod
    def from_environment(cls, *, required=False):
        """Validate fresh absolute supervisor paths without publishing anything.

        Return ``None`` when both variables are absent for standalone diagnostics,
        unless ``required`` is true. Partial configuration, reused epoch paths,
        and invalid startup timeouts fail before any files are created.
        """
        manifest = os.environ.get('G3_RECOVERY_MANIFEST')
        ack = os.environ.get('G3_RECOVERY_ACK')
        if manifest is None and ack is None:
            if required:
                raise RuntimeError('supervisor registration is required')
            return None
        if not manifest or not ack:
            raise RuntimeError('recovery manifest and ACK must be supplied together')
        manifest, ack = Path(manifest), Path(ack)
        temporary = Path(str(manifest) + '.tmp')
        if not manifest.is_absolute() or not ack.is_absolute():
            raise RuntimeError('recovery registration paths must be absolute')
        if manifest.exists() or temporary.exists() or ack.exists():
            raise RuntimeError('recovery registration must use a new epoch')
        timeout = float(os.environ.get('G3_RECOVERY_START_TIMEOUT', '10'))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('recovery startup timeout must be finite and positive')
        return cls(manifest, ack, timeout)

    def wait(self, resources):
        """Atomically publish this PID's complete inventory, then wait for ACK.

        ``resources`` is the complete owned metadata/socket path list; CPU peers
        and consumer-only peers supply an empty list. Timeout preserves the
        inventory for owner cleanup and raises ``TimeoutError``.
        """
        if self._started:
            raise RuntimeError('recovery registration already started')
        self._started = True
        temporary = Path(str(self.manifest) + '.tmp')
        with temporary.open('x', encoding='utf-8') as output:
            json.dump({'version': 1, 'pid': os.getpid(), 'resources': resources}, output)
            output.write('\n')
        temporary.replace(self.manifest)
        deadline = time.monotonic() + self.timeout
        while not self.ack.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('recovery registration acknowledgement timed out')
            time.sleep(0.005)
