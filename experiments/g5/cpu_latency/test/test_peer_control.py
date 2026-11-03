"""Registration and loss-tolerant observation boundaries without launching ROS."""

import json
from types import SimpleNamespace

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


def test_measurement_end_snapshot_keeps_loss_and_order_evidence(monkeypatch):
    report = {'samples': [[0, 1, 2], [2, 3, 4], [4, 5, 6]]}
    monkeypatch.setattr(peer, 'snapshot', lambda **kwargs: {'minor_page_faults': 17})
    peer.observe_sample_boundary(report, 4, warmup=2, samples=3)
    assert report['resources_measurement_end'] == {'minor_page_faults': 17}
    assert report['measurement_end_snapshot_sequence'] == 4
    assert report['measurement_end_snapshot_count'] == 3
    assert report['measurement_end_measured_count'] == 2
    assert not report['measurement_end_complete']


def test_measurement_end_snapshot_is_not_overwritten_by_late_receipts(monkeypatch):
    report = {'samples': [[0, 1, 2], [2, 3, 4]]}
    calls = []
    monkeypatch.setattr(peer, 'snapshot', lambda **kwargs: calls.append(1) or {})
    peer.observe_sample_boundary(report, 2, warmup=1, samples=2)
    report['samples'].append([1, 5, 6])
    peer.observe_sample_boundary(report, 1, warmup=1, samples=2)
    assert report['measurement_end_snapshot_count'] == 2
    assert report['measurement_end_measured_count'] == 1
    assert not report['measurement_end_complete']
    assert len(calls) == 2


def test_complete_measurement_boundary_excludes_warmup_count(monkeypatch):
    report = {'samples': [[i, 1, 2] for i in range(5)]}
    monkeypatch.setattr(peer, 'snapshot', lambda **kwargs: {})
    peer.observe_sample_boundary(report, 4, warmup=2, samples=3)
    assert report['measurement_end_snapshot_count'] == 5
    assert report['measurement_end_measured_count'] == 3
    assert report['measurement_end_complete']


def test_lost_final_sequence_does_not_fabricate_measurement_end(monkeypatch):
    report = {'samples': [[0, 1, 2], [1, 3, 4]]}
    monkeypatch.setattr(peer, 'snapshot', lambda **kwargs: {})
    peer.observe_sample_boundary(report, 1, warmup=1, samples=2)
    assert 'resources_measurement_end' not in report


def test_snapshot_brackets_its_own_resource_cost(tmp_path, monkeypatch):
    (tmp_path / 'status').write_text('VmRSS: 8 kB\nVmHWM: 8 kB\nVmSize: 16 kB\nThreads: 1\n')
    (tmp_path / 'fd').mkdir()
    monkeypatch.setattr(peer, 'Path', lambda name: tmp_path / name.rsplit('/', 1)[1])
    monkeypatch.setattr(peer, 'memory_snapshot', lambda: {})
    monkeypatch.setattr(peer, 'cpu_frequency_snapshot', lambda: None)
    monkeypatch.setattr(peer, 'thread_snapshot', lambda: [])
    moments = iter((10, 20, 30, 40))
    monkeypatch.setattr(peer.time, 'monotonic_ns', lambda: next(moments))
    usages = iter((SimpleNamespace(ru_utime=1, ru_stime=2, ru_nvcsw=3, ru_nivcsw=4,
                                  ru_minflt=5, ru_majflt=6),
                   SimpleNamespace(ru_utime=11, ru_stime=12, ru_nvcsw=13, ru_nivcsw=14,
                                   ru_minflt=15, ru_majflt=16)))
    monkeypatch.setattr(peer.resource, 'getrusage', lambda _: next(usages))
    result = peer.snapshot()
    assert result['minor_page_faults'] == 5
    assert result['cpu_user_seconds'] == 1
    assert result['usage_collected_ns'] == 20
    assert result['post_collection_usage'] == {
        'cpu_user_seconds': 11, 'cpu_system_seconds': 12,
        'voluntary_context_switches': 13, 'involuntary_context_switches': 14,
        'minor_page_faults': 15, 'major_page_faults': 16, 'usage_collected_ns': 30}
    assert result['collection_started_ns'] == 10
    assert result['collection_finished_ns'] == 40


@pytest.mark.parametrize('detailed', [False, True])
def test_light_snapshot_never_collects_smaps_thread_or_frequency_data(monkeypatch, detailed):
    calls = []
    monkeypatch.setattr(peer, 'memory_snapshot', lambda: calls.append('memory') or {})
    monkeypatch.setattr(peer, 'cpu_frequency_snapshot', lambda: calls.append('frequency') or [])
    monkeypatch.setattr(peer, 'thread_snapshot', lambda: calls.append('threads') or [])
    result = peer.snapshot(detailed=detailed)
    assert calls == (['memory', 'frequency', 'threads'] if detailed else [])
    assert result['detailed'] is detailed
    assert 'minor_page_faults' in result and 'fds' in result
    assert result['collection_started_ns'] <= result['usage_collected_ns']
    assert result['post_collection_usage']['usage_collected_ns'] <= result['collection_finished_ns']
    if not detailed:
        assert result['memory'] is result['cpu_frequency'] is result['threads'] is None


@pytest.mark.parametrize('diagnostic', [False, True])
def test_measurement_boundary_collects_heavy_resources_only_when_explicit(monkeypatch, diagnostic):
    report = {'samples': [[0, 1, 2]]}
    calls = []
    monkeypatch.setattr(peer, 'snapshot', lambda **kwargs: calls.append(kwargs) or {})
    options = {'resource_diagnostics': True} if diagnostic else {}
    peer.observe_sample_boundary(report, 0, warmup=1, samples=2, **options)
    assert calls == [{'network': False, 'detailed': diagnostic}]
