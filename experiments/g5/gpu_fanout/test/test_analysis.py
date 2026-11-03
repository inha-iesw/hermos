"""Expose branch loss and prohibit survivor-only latency acceptance."""

import pytest

from experiments.g5.gpu_fanout.analysis import analyze_fanout, paired_effects, report


def records():
    publisher = dict(time_namespace='time:[1]', samples=[[i, 100*i, 100*i+10, 100*i+20]
                                                      for i in range(4)])
    first = dict(time_namespace='time:[1]', samples=[[i, 100*i+30, 100*i+50]
                                                  for i in range(4)])
    second = dict(time_namespace='time:[1]', samples=[[i, 100*i+40, 100*i+80]
                                                   for i in (0, 1, 3)])
    return publisher, [first, second]


def case(transport='hermos', **changes):
    return dict(name=transport, transport=transport, size=1024, subscribers=2,
                rmw='test', rate_hz=10, repeat=1, warmup=1, samples=3,
                period_ns=100) | changes


def test_aggregate_loss_does_not_hide_incomplete_branch_or_common_ids():
    publisher, subscribers = records()
    result = analyze_fanout(case(), publisher, subscribers)
    assert not result['complete'] and result['lost'] == 1
    assert result['received'] == [3, 2]
    assert result['aggregate_lost_fraction'] == pytest.approx(1/6)
    assert result['worst_subscriber_lost_fraction'] == pytest.approx(1/3)
    assert result['all_subscribers_received_ids'] == [1, 3]
    assert result['all_subscribers_completion_ns']['mean'] == 80
    assert result['all_subscribers_period_unsatisfied'] == 1
    assert result['per_subscriber'][1]['missing_sequences'] == [2]
    assert result['per_subscriber'][1]['longest_loss_burst'] == 1
    assert result['sender_drift_ns'] == 0


def test_loss_invalidates_pair_and_repeat_identity_is_not_pooled():
    publisher, subscribers = records()
    lost = analyze_fanout(case('vanilla'), publisher, subscribers)
    full = analyze_fanout(case(), publisher, [subscribers[0], subscribers[0]])
    comparison = paired_effects([lost, full])[0]
    assert comparison['valid'] is False and 'loss' in comparison['reason']
    full['case']['repeat'] = 2
    assert paired_effects([lost, full])[0]['reason'] == 'missing baseline'


def test_no_receipts_remain_unavailable_completion_not_zero_latency():
    publisher, subscribers = records()
    subscribers[1]['samples'] = []
    result = analyze_fanout(case(), publisher, subscribers)
    assert result['all_subscribers_completion_ns'] is None
    assert result['all_subscribers_received_ids'] == []
    assert result['all_subscribers_period_unsatisfied'] == 3


def test_clock_namespace_mismatch_rejects_comparison():
    publisher, subscribers = records()
    subscribers[1]['time_namespace'] = 'time:[2]'
    with pytest.raises(ValueError, match='clock'):
        analyze_fanout(case(), publisher, subscribers)


def test_single_sample_smoke_report_keeps_sender_rate_unavailable(tmp_path):
    publisher, subscribers = records()
    publisher['samples'] = publisher['samples'][:2]
    for subscriber in subscribers:
        subscriber['samples'] = subscriber['samples'][:2]
    result = analyze_fanout(case(samples=1), publisher, subscribers)
    assert result['observed_send_hz'] is None
    report([result], tmp_path, {'samples': 1})
    text = (tmp_path / 'report.md').read_text()
    assert '1 scheduled sample' in text and 'unavailable' in text
