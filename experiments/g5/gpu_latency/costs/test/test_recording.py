"""Cost instrumentation delegates operations and retains only numeric evidence."""

import gc
import json
from types import SimpleNamespace
import weakref

import pytest

from experiments.g5.gpu_latency.costs.peer import Recorder, NativeRuntimeProxy, fill_with_costs
from experiments.g5.gpu_latency.costs.analysis import summarize
from experiments.g5.gpu_latency.costs import peer as cost_peer


def test_recorder_records_exact_boundaries_and_returns_the_original_object():
    ticks = iter((10, 25))
    recorder = Recorder(clock=lambda: next(ticks))
    recorder.begin()
    owner = SimpleNamespace(value=1)
    assert recorder.call('prepare', lambda: owner) is owner
    recorder.end(7)
    assert recorder.samples == [dict(sequence=7, events=[
        dict(stage='prepare', started_ns=10, finished_ns=25, failed=False)])]


def test_recording_exception_propagates_and_does_not_retain_native_owner():
    ticks = iter((10, 25))
    recorder = Recorder(clock=lambda: next(ticks))
    recorder.begin()

    class Owner:
        def fail(self):
            raise RuntimeError('native failure')

    owner = Owner()
    reference = weakref.ref(owner)
    with pytest.raises(RuntimeError, match='native failure'):
        recorder.call('map', owner.fail)
    del owner
    gc.collect()
    assert reference() is None
    recorder.end(1)
    assert recorder.samples[0]['events'][0]['failed'] is True


def test_inactive_recorder_delegates_without_reading_any_clock():
    recorder = Recorder(clock=lambda: pytest.fail('clock outside active sample'))
    assert recorder.call('map', lambda value: value, 4) == 4
    assert recorder.samples == []


def test_runtime_proxy_preserves_borrow_arguments_prepare_dlpack_cancel_and_mapping():
    calls = []
    native_loan = SimpleNamespace(
        prepare=lambda: calls.append('prepare') or {'metadata': 1},
        dlpack=lambda stream: calls.append(('dlpack', stream)) or 'capsule',
        cancel=lambda: calls.append('cancel'), state='BORROWED')
    native = SimpleNamespace(
        borrow=lambda *args: calls.append(('borrow', args)) or native_loan,
        map=lambda *args: calls.append(('map', args)) or native_loan,
        poll=lambda: calls.append('poll'))
    ticks = iter((1, 2, 3, 5))
    recorder = Recorder(clock=lambda: next(ticks))
    proxy = NativeRuntimeProxy(native, recorder)
    loan = proxy.borrow('shape', 1, 8, 123, 7)
    assert loan.state == 'BORROWED'
    assert loan.dlpack(123) == 'capsule'
    recorder.begin()
    assert loan.prepare() == {'metadata': 1}
    assert proxy.map('descriptor', 123, 7) is native_loan
    loan.cancel()
    proxy.poll()
    recorder.end(0)
    assert calls == [('borrow', ('shape', 1, 8, 123, 7)), ('dlpack', 123),
                     'prepare', ('map', ('descriptor', 123, 7)), 'cancel', 'poll']
    assert [event['stage'] for event in recorder.samples[0]['events']] == ['prepare', 'map']


def test_mapping_result_owner_is_not_retained_by_the_recorder():
    class Owner:
        pass

    recorder = Recorder()
    proxy = NativeRuntimeProxy(SimpleNamespace(map=lambda: Owner()), recorder)
    recorder.begin()
    owner = proxy.map()
    reference = weakref.ref(owner)
    del owner
    gc.collect()
    assert reference() is None
    recorder.end(0)


@pytest.mark.parametrize('fail', [False, True])
def test_metadata_owner_is_unchanged_during_fill_success_or_exception(fail):
    calls = []
    owner = SimpleNamespace(resize=lambda *args: calls.append(('resize', args)), marker=9)
    message = SimpleNamespace(_owner=owner)
    recorder = Recorder(clock=iter((1, 2, 4, 8)).__next__)
    recorder.begin()

    def fill(value, descriptor):
        assert message._owner is owner
        assert value is not message and value._owner.marker == 9 and descriptor == {'field': 1}
        value._owner.resize((1,), 0, 4)
        if fail:
            raise ValueError('bad field')

    if fail:
        with pytest.raises(ValueError, match='bad field'):
            fill_with_costs(recorder, fill, message, {'field': 1})
    else:
        fill_with_costs(recorder, fill, message, {'field': 1})
    assert message._owner is owner
    recorder.end(0)
    assert calls == [('resize', ((1,), 0, 4))]
    assert [event['stage'] for event in recorder.samples[0]['events']] == [
        'descriptor_resize', 'descriptor_fill_total']
    assert recorder.samples[0]['events'][-1]['failed'] is fail


def test_cost_analysis_excludes_warmup_and_subtracts_only_nested_resizes():
    def sample(sequence, offset):
        return dict(sequence=sequence, events=[
            dict(stage='descriptor_resize', started_ns=offset + 2, finished_ns=offset + 5, failed=False),
            dict(stage='descriptor_fill_total', started_ns=offset, finished_ns=offset + 10, failed=False)])
    result = summarize([sample(0, 0), sample(1, 100)], warmup=1, samples=1)
    assert result['measured_samples'] == 1
    assert result['stages']['descriptor_fill_total']['per_sample_ns']['mean'] == 10
    assert result['stages']['descriptor_resize']['calls'] == 1
    assert result['stages']['descriptor_fill_excluding_resize']['per_sample_ns']['mean'] == 7
    assert 'prepare' not in result['stages']


def test_cost_analysis_rejects_resize_time_larger_than_enclosing_fill():
    events = [dict(stage='descriptor_resize', started_ns=0, finished_ns=20, failed=False),
              dict(stage='descriptor_fill_total', started_ns=0, finished_ns=10, failed=False)]
    with pytest.raises(ValueError, match='resize'):
        summarize([dict(sequence=0, events=events)], warmup=0, samples=1)


def test_cost_factory_rejects_unlabeled_run_before_installing_instrumentation(tmp_path, monkeypatch):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'samples': 1}))
    monkeypatch.setattr(cost_peer, 'install', lambda *args: pytest.fail('installed in headline run'))
    with pytest.raises(ValueError, match='diagnostic'):
        cost_peer.factory(object(), ['publisher', str(config), str(tmp_path / 'publisher.json')])


@pytest.mark.parametrize('map_finished,expected', [(7, 6), (12, None)])
def test_export_remainder_requires_disjoint_children_inside_the_total(map_finished, expected):
    events = [dict(stage='descriptor_decode', started_ns=1, finished_ns=3, failed=False),
              dict(stage='map', started_ns=5, finished_ns=map_finished, failed=False),
              dict(stage='tensor_export_total', started_ns=0, finished_ns=10, failed=False)]
    result = summarize([dict(sequence=0, events=events)], warmup=0, samples=1)
    if expected is None:
        assert 'tensor_export_remaining' not in result['stages']
        assert result['exclusive_export_unavailable_samples'] == 1
    else:
        assert result['stages']['tensor_export_remaining']['per_sample_ns']['mean'] == expected
        assert result['exclusive_export_unavailable_samples'] == 0
