"""Device tests own fresh namespaces through the existing verified session runner."""

import os
from pathlib import Path
import tempfile

import pytest

from isolated import IsolatedEpoch
from support import inventory


@pytest.fixture
def epochs(tmp_path):
    if os.environ.get('G4_RUN_DEVICE') != '1':
        pytest.skip('requires G4_RUN_DEVICE=1 inside the verified device session')
    if not __debug__:
        raise RuntimeError('G4 verification cannot run with Python optimization')
    root = Path(os.environ.get('G3_PEER_LOG_ROOT', tmp_path))
    root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='g4-', dir=root)).resolve()
    baseline = inventory()
    active = []

    def start(commands):
        epoch = IsolatedEpoch(commands, stage / f'epoch-{len(active)}',
                              startup_timeout=30, stop_timeout=5)
        active.append(epoch)
        epoch.start()
        current = inventory()
        assert all(current.get(name) == value for name, value in baseline.items())
        # A test-owned outside DDS participant can temporarily create its own
        # SHM objects. The final full inventory still has to match exactly.
        assert {name: value for name, value in current.items() if name.startswith('cuda.shm.')} == {
            name: value for name, value in baseline.items() if name.startswith('cuda.shm.')}
        return epoch

    yield start
    errors = []
    for epoch in reversed(active):
        try:
            record = epoch.stop()
            assert record['verified_teardown'] and record['echild'], record
            assert all(item['returncode'] == 0 for item in record['descendants']), record
        except BaseException as error:
            errors.append(f'{epoch.stage}: {error!r}')
    assert inventory() == baseline
    assert not errors, errors
