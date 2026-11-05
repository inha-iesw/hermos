"""Registration validates first, then publishes a complete inventory before ACK."""

import json
import os

import pytest

from hermos_runtime.supervision import registration


@pytest.fixture(autouse=True)
def clean_registration_environment(monkeypatch):
    for name in ('G3_RECOVERY_MANIFEST', 'G3_RECOVERY_ACK', 'G3_RECOVERY_START_TIMEOUT'):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def paths(tmp_path, monkeypatch):
    manifest, ack = tmp_path / 'peer.json', tmp_path / 'peer.ack'
    monkeypatch.setenv('G3_RECOVERY_MANIFEST', str(manifest))
    monkeypatch.setenv('G3_RECOVERY_ACK', str(ack))
    return manifest, ack


def test_standalone_diagnostics_need_no_registration():
    assert registration.Registration.from_environment() is None


def test_required_registration_rejects_missing_supervisor():
    with pytest.raises(RuntimeError, match='required'):
        registration.Registration.from_environment(required=True)


@pytest.mark.parametrize('name', ['G3_RECOVERY_MANIFEST', 'G3_RECOVERY_ACK'])
def test_partial_environment_rejects_before_writing(paths, monkeypatch, name):
    monkeypatch.delenv(name)
    with pytest.raises(RuntimeError, match='together'):
        registration.Registration.from_environment()
    assert not any(path.exists() for path in paths)


@pytest.mark.parametrize('name', ['G3_RECOVERY_MANIFEST', 'G3_RECOVERY_ACK'])
@pytest.mark.parametrize('value', ['', 'relative'])
def test_invalid_path_rejects_before_writing(paths, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match='together|absolute'):
        registration.Registration.from_environment()
    assert not any(path.exists() for path in paths)


@pytest.mark.parametrize('occupied', ['peer.json', 'peer.json.tmp', 'peer.ack'])
def test_previous_epoch_is_preserved(paths, occupied):
    previous = paths[0].parent / occupied
    previous.write_text('previous epoch')
    with pytest.raises(RuntimeError, match='new epoch'):
        registration.Registration.from_environment()
    assert previous.read_text() == 'previous epoch'


@pytest.mark.parametrize('timeout', ['0', '-1', 'nan', 'inf', 'invalid'])
def test_invalid_timeout_never_publishes_inventory(paths, monkeypatch, timeout):
    monkeypatch.setenv('G3_RECOVERY_START_TIMEOUT', timeout)
    with pytest.raises(ValueError):
        registration.Registration.from_environment()
    assert not any(paths[0].parent.iterdir())


def test_inventory_is_atomic_and_ack_is_required_before_return(paths, monkeypatch):
    manifest, ack = paths
    resources = ['/dev/shm/rclpy_zc_gpu_v1_123_0', '/tmp/cuda_memory_pool_test.sock']
    request = registration.Registration.from_environment()
    assert not any(manifest.parent.iterdir())

    def acknowledge(_):
        assert json.loads(manifest.read_text()) == {
            'version': 1, 'pid': os.getpid(), 'resources': resources}
        assert not manifest.with_suffix('.json.tmp').exists()
        ack.write_text('registered\n')

    monkeypatch.setattr(registration.time, 'sleep', acknowledge)
    request.wait(resources)
    assert ack.exists()


def test_missing_ack_preserves_manifest_and_times_out(paths, monkeypatch):
    manifest, _ = paths
    monkeypatch.setenv('G3_RECOVERY_START_TIMEOUT', '1')
    moments = iter((10, 11))
    monkeypatch.setattr(registration.time, 'monotonic', lambda: next(moments))
    request = registration.Registration.from_environment()
    with pytest.raises(TimeoutError, match='acknowledgement'):
        request.wait([])
    assert json.loads(manifest.read_text())['resources'] == []


def test_second_wait_cannot_replace_an_existing_inventory(paths, monkeypatch):
    manifest, ack = paths
    request = registration.Registration.from_environment()
    monkeypatch.setattr(registration.time, 'sleep', lambda _: ack.touch())
    request.wait([])
    previous = manifest.read_bytes()
    with pytest.raises(RuntimeError, match='already'):
        request.wait(['replacement'])
    assert manifest.read_bytes() == previous
