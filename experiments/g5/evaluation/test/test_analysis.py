"""Independent oracles for loss bursts, pacing and bounded resource windows."""

import pytest

from experiments.g5.evaluation.analysis import evaluate, resource_window, compare


def records():
    publisher = {'samples': [[i, 100 + i * 10, 102 + i * 10, 105 + i * 10]
                             for i in range(6)]}
    subscriber = {'samples': [[0, 104, 106], [1, 114, 120], [4, 144, 151]]}
    return publisher, subscriber


def test_loss_bursts_and_period_budget_include_missing_messages():
    publisher, subscriber = records()
    result = evaluate(publisher, subscriber, warmup=1, samples=5, period_ns=10)
    assert result['missing_sequences'] == [2, 3, 5]
    assert result['longest_loss_burst'] == 2
    assert result['consumer_period_exceedances'] == 1
    assert result['period_budget_unsatisfied'] == 4
    assert result['consumer_within_period'] == 1  # equality is within budget
    assert result['callback_gap_ns']['max'] == 30
    assert result['sender_drift_ns'] == 0
    assert result['fill_ns']['mean'] == 2
    assert result['publish_call_ns']['mean'] == 3


def test_no_receipts_has_no_invented_latency_or_gap():
    publisher, _ = records()
    result = evaluate(publisher, {'samples': []}, warmup=1, samples=5, period_ns=10)
    assert result['longest_loss_burst'] == 5
    assert result['period_budget_unsatisfied'] == 5
    assert result['callback_gap_ns'] is None
    assert result['completion_ns'] is None


def test_receipt_reordering_uses_timestamps_and_does_not_look_like_loss():
    publisher, _ = records()
    subscriber = {'samples': [[i, 200 + (5 - i) * 10, 201 + (5 - i) * 10]
                              for i in range(6)]}
    result = evaluate(publisher, subscriber, warmup=1, samples=5, period_ns=10)
    assert result['complete']
    assert result['reordered_receipts'] == 4


def test_incomplete_case_cannot_win_paired_latency_comparison():
    current = {'complete': False, 'completion_ns': {'mean': 1, 'p99': 1}}
    baseline = {'complete': True, 'completion_ns': {'mean': 10, 'p99': 10}}
    assert compare(current, baseline) == {'valid': False, 'reason': 'receipt loss or invalid chain'}


def test_paired_effect_is_signed_extra_latency():
    current = {'complete': True, 'completion_ns': {'mean': 12, 'p99': 30}}
    baseline = {'complete': True, 'completion_ns': {'mean': 10, 'p99': 20}}
    result = compare(current, baseline)
    assert result['mean_change_percent'] == pytest.approx(20)
    assert result['p99_change_percent'] == pytest.approx(50)


def test_historical_resource_cost_is_explicitly_measurement_plus_drain():
    report, _ = records()
    report.update(warmup_snapshot_sequence=0,
                  resources_after_warmup={'cpu_user_seconds': 1., 'cpu_system_seconds': 2.},
                  resources_after={'cpu_user_seconds': 1.01, 'cpu_system_seconds': 2.02})
    result = resource_window(report, warmup=1)
    assert result['scope'] == 'post-warmup through finish/drain'
    assert result['observed_rows'] == 5
    assert result['cpu_seconds_per_observed_row'] == pytest.approx(.006)
    assert result['minor_page_faults_per_observed_row'] is None


def test_resource_denominator_uses_observed_row_boundary_not_sequence_number():
    report = dict(samples=[[2, 10, 11], [1, 20, 21], [4, 30, 31]],
                  warmup_snapshot_sequence=2, warmup_snapshot_count=1,
                  measurement_end_snapshot_count=3, measurement_end_complete=False,
                  resources_after_warmup={'minor_page_faults': 10},
                  resources_measurement_end={'minor_page_faults': 14})
    result = resource_window(report, warmup=1)
    assert result['observed_rows'] == 2
    assert result['complete_window'] is False
    assert result['minor_page_faults_per_observed_row'] == 2


def test_decreasing_resource_counter_is_invalid_evidence():
    report, _ = records()
    report.update(warmup_snapshot_sequence=0,
                  resources_after_warmup={'minor_page_faults': 5},
                  resources_after={'minor_page_faults': 4})
    with pytest.raises(ValueError, match='decreased'):
        resource_window(report, warmup=1)


def test_missing_resource_evidence_is_not_zero_cost():
    report, _ = records()
    assert resource_window(report, warmup=1)['available'] is False


def test_resource_window_excludes_warmup_observation_cost():
    report, _ = records()
    report.update(warmup_snapshot_sequence=0,
                  resources_after_warmup={'minor_page_faults': 5,
                                          'post_collection_usage': {'minor_page_faults': 12}},
                  resources_after={'minor_page_faults': 22})
    result = resource_window(report, warmup=1)
    assert result['excludes_warmup_collection']
    assert result['minor_page_faults_per_observed_row'] == 2


def test_late_warmup_receipts_are_counted_in_resource_work_and_labelled():
    report = dict(samples=[[2, 10, 11], [0, 20, 21], [1, 30, 31], [3, 40, 41]],
                  warmup_snapshot_sequence=2, warmup_snapshot_count=1,
                  measurement_end_snapshot_count=4, measurement_end_complete=True,
                  resources_after_warmup={'minor_page_faults': 10},
                  resources_measurement_end={'minor_page_faults': 16})
    result = resource_window(report, warmup=2)
    assert result['observed_rows'] == 3
    assert result['measured_rows'] == 1
    assert result['late_warmup_rows'] == 2
    assert result['minor_page_faults_per_observed_row'] == 2


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1, True])
def test_resource_counters_must_be_finite_nonnegative_numbers(value):
    report, _ = records()
    report.update(warmup_snapshot_sequence=0,
                  resources_after_warmup={'minor_page_faults': 0},
                  resources_after={'minor_page_faults': value})
    with pytest.raises(ValueError, match='invalid resource counter'):
        resource_window(report, warmup=1)


def test_each_receipt_below_previous_sequence_high_water_is_reordered():
    publisher, _ = records()
    subscriber = {'samples': [[seq, 200 + i * 10, 201 + i * 10]
                              for i, seq in enumerate([0, 3, 1, 2, 4, 5])]}
    assert evaluate(publisher, subscriber, warmup=0, samples=6,
                    period_ns=10)['reordered_receipts'] == 2


def test_detailed_boundary_collection_does_not_establish_headline_speedup():
    result = {'complete': True, 'completion_ns': {'mean': 1, 'p99': 1},
              'resource_diagnostics': True}
    assert compare(result, result) == {'valid': False, 'reason': 'resource-diagnostic timing'}


def test_cost_instrumentation_on_either_side_invalidates_headline_speedup():
    plain = {'complete': True, 'completion_ns': {'mean': 1, 'p99': 1}}
    diagnostic = dict(plain, timing_diagnostics=True)
    for left, right in ((diagnostic, plain), (plain, diagnostic)):
        assert compare(left, right) == {'valid': False, 'reason': 'cost-diagnostic timing'}
