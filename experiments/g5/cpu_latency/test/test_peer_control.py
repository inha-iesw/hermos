"""Registration and loss-tolerant observation boundaries without launching ROS."""

import json

import pytest

from experiments.g5.cpu_latency import peer


@pytest.mark.parametrize('timeout', ['0', '-1', 'nan', 'inf'])
def test_invalid_registration_timeout_does_not_publish_manifest(tmp_path, monkeypatch, timeout):
    manifest = tmp_path / 'manifest.json'
    monkeypatch.setenv('G3_RECOVERY_MANIFEST', str(manifest))
    monkeypatch.setenv('G3_RECOVERY_ACK', str(tmp_path / 'ack'))
    monkeypatch.setenv('G3_RECOVERY_START_TIMEOUT', timeout)
    with pytest.raises(ValueError, match='finite and positive'):
        peer.register()
    assert not manifest.exists()
    assert not (tmp_path / 'manifest.json.tmp').exists()


def test_registration_requires_ack_after_atomic_empty_inventory(tmp_path, monkeypatch):
    manifest, ack = tmp_path / 'manifest.json', tmp_path / 'ack'
    monkeypatch.setenv('G3_RECOVERY_MANIFEST', str(manifest))
    monkeypatch.setenv('G3_RECOVERY_ACK', str(ack))

    def acknowledge(_):
        value = json.loads(manifest.read_text())
        assert value == {'version': 1, 'pid': peer.os.getpid(), 'resources': []}
        assert not (tmp_path / 'manifest.json.tmp').exists()
        ack.write_text('registered\n')

    monkeypatch.setattr(peer.time, 'sleep', acknowledge)
    peer.register()
    assert ack.exists()


def test_registration_times_out_at_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv('G3_RECOVERY_MANIFEST', str(tmp_path / 'manifest.json'))
    monkeypatch.setenv('G3_RECOVERY_ACK', str(tmp_path / 'ack'))
    monkeypatch.setenv('G3_RECOVERY_START_TIMEOUT', '1')
    moments = iter((10, 11))
    monkeypatch.setattr(peer.time, 'monotonic', lambda: next(moments))
    with pytest.raises(TimeoutError, match='acknowledgement'):
        peer.register()


def test_warmup_snapshot_uses_next_received_sequence_when_boundary_was_lost():
    assert not peer.warmup_snapshot_due({}, 98, 100)
    assert peer.warmup_snapshot_due({}, 102, 100)
    assert not peer.warmup_snapshot_due({'resources_after_warmup': {}}, 103, 100)


def test_zero_warmup_observes_first_receipt_only():
    assert peer.warmup_snapshot_due({}, 0, 0)
    assert not peer.warmup_snapshot_due({'resources_after_warmup': {}}, 1, 0)
