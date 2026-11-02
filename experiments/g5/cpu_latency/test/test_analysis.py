"""Independent oracles for measured latency joins and incomplete delivery."""

from copy import deepcopy

import pytest

from experiments.g5.cpu_latency.analysis import analyze


def records():
    return ({'samples': [
        [0, 0, 1, 2],
        [1, 100, 105, 111],
        [2, 200, 210, 220],
        [3, 300, 320, 340],
    ]}, {'samples': [
        [0, 90, 91],
        [1, 115, 118],
        [2, 230, 235],
        [3, 360, 370],
    ]})


def test_warmup_is_excluded_and_known_distributions_are_reported():
    publisher, subscriber = records()
    result = analyze(publisher, subscriber, warmup=1, samples=3)

    assert (result['expected'], result['received'], result['lost']) == (3, 3, 0)
    assert result['complete'] is True
    latency = result['publish_to_callback_ns']
    assert latency == pytest.approx({
        'count': 3, 'mean': 70 / 3, 'min': 10, 'max': 40,
        'variance': 1400 / 9, 'p50': 20, 'p99': 39.6,
    })
    assert result['fill_to_consumer_ns'] == pytest.approx({
        'count': 3, 'mean': 41, 'min': 18, 'max': 70,
        'variance': 1406 / 3, 'p50': 35, 'p99': 69.3,
    })
    assert result['publish_call_ns'] == pytest.approx({
        'count': 3, 'mean': 12, 'min': 6, 'max': 20,
        'variance': 104 / 3, 'p50': 10, 'p99': 19.8,
    })
    assert result['observation_window_ns'] == 245
    assert result['observed_delivery_per_second'] == pytest.approx(2e9 / 245)


def test_join_uses_sequence_instead_of_receipt_or_file_order():
    publisher, subscriber = records()
    expected = analyze(publisher, subscriber, warmup=1, samples=3)
    publisher['samples'].reverse()
    subscriber['samples'] = [subscriber['samples'][i] for i in (3, 1, 0, 2)]

    assert analyze(publisher, subscriber, warmup=1, samples=3) == expected


def test_loss_is_explicit_and_does_not_filter_publish_call_cost():
    publisher, subscriber = records()
    del subscriber['samples'][2]

    result = analyze(publisher, subscriber, warmup=1, samples=3)

    assert (result['expected'], result['received'], result['lost']) == (3, 2, 1)
    assert result['complete'] is False
    assert result['publish_to_callback_ns']['count'] == 2
    assert result['publish_to_callback_ns']['mean'] == 25
    assert result['publish_call_ns']['count'] == 3
    assert result['publish_call_ns']['mean'] == 12


def test_missing_warmup_receipts_do_not_count_as_measured_loss():
    publisher, subscriber = records()
    del subscriber['samples'][0]

    result = analyze(publisher, subscriber, warmup=1, samples=3)

    assert result['complete'] is True
    assert result['lost'] == 0


def test_callback_may_begin_before_publish_returns():
    result = analyze({'samples': [[0, 90, 100, 150]]},
                     {'samples': [[0, 110, 120]]}, warmup=0, samples=1)

    assert result['publish_to_callback_ns']['mean'] == 10
    assert result['publish_call_ns']['mean'] == 50
    assert result['publish_to_callback_ns']['variance'] == 0


def test_zero_observation_window_has_no_infinite_delivery_rate():
    result = analyze({'samples': [[0, 10, 10, 10]]},
                     {'samples': [[0, 10, 10]]}, warmup=0, samples=1)

    assert result['publish_to_callback_ns']['mean'] == 0
    assert result['observation_window_ns'] == 0
    assert result['observed_delivery_per_second'] is None


@pytest.mark.parametrize('subscriber_rows', [[], [[0, 90, 91]]])
def test_no_measured_receipts_cannot_produce_statistics(subscriber_rows):
    publisher, _ = records()
    result = analyze(publisher, {'samples': subscriber_rows}, warmup=1, samples=3)
    assert (result['expected'], result['received'], result['lost']) == (3, 0, 3)
    assert result['complete'] is False
    assert result['publish_to_callback_ns'] is None
    assert result['fill_to_consumer_ns'] is None
    assert result['publish_call_ns']['mean'] == 12
    assert result['observation_window_ns'] is None
    assert result['observed_delivery_per_second'] is None


def test_delivery_rate_counts_intervals_without_fixed_latency_bias():
    publisher = {'samples': [[i, i * 100, i * 100, i * 100 + 2] for i in range(3)]}
    subscriber = {'samples': [[i, i * 100 + 50, i * 100 + 55] for i in range(3)]}
    result = analyze(publisher, subscriber, warmup=0, samples=3)
    assert result['observation_window_ns'] == 200
    assert result['observed_delivery_per_second'] == 10000000


def test_one_receipt_cannot_establish_a_delivery_rate():
    publisher, subscriber = records()
    subscriber['samples'] = subscriber['samples'][1:2]
    result = analyze(publisher, subscriber, warmup=1, samples=3)
    assert result['observation_window_ns'] == 0
    assert result['observed_delivery_per_second'] is None


@pytest.mark.parametrize('owner', ['publisher', 'subscriber'])
def test_duplicate_sequence_is_rejected(owner):
    publisher, subscriber = records()
    target = publisher if owner == 'publisher' else subscriber
    target['samples'].append(target['samples'][1][:])
    with pytest.raises(ValueError, match='duplicate'):
        analyze(publisher, subscriber, warmup=1, samples=3)


def test_missing_publisher_record_cannot_be_treated_as_delivery_loss():
    publisher, subscriber = records()
    del publisher['samples'][2]
    with pytest.raises(ValueError, match='publisher.*sequence'):
        analyze(publisher, subscriber, warmup=1, samples=3)


def test_unsent_subscriber_sequence_is_rejected():
    publisher, subscriber = records()
    subscriber['samples'].append([4, 400, 410])
    with pytest.raises(ValueError, match='subscriber.*sequence'):
        analyze(publisher, subscriber, warmup=1, samples=3)


@pytest.mark.parametrize('field,value', [
    ('warmup', -1), ('warmup', True), ('warmup', 0.0),
    ('samples', 0), ('samples', -1), ('samples', False), ('samples', '3'),
])
def test_invalid_measurement_counts_are_rejected(field, value):
    publisher, subscriber = records()
    arguments = {'warmup': 1, 'samples': 3, field: value}
    with pytest.raises(ValueError, match=field):
        analyze(publisher, subscriber, **arguments)


@pytest.mark.parametrize('owner,index,column,value', [
    ('publisher', 1, 0, -1), ('publisher', 1, 0, True),
    ('subscriber', 1, 0, 1.0), ('subscriber', 1, 1, -1),
    ('publisher', 1, 1, True), ('publisher', 1, 2, '105'),
    ('publisher', 1, 3, 111.0), ('subscriber', 1, 2, None),
])
def test_record_values_must_be_nonnegative_integers(owner, index, column, value):
    publisher, subscriber = records()
    target = publisher if owner == 'publisher' else subscriber
    target['samples'][index][column] = value
    with pytest.raises(ValueError, match=owner):
        analyze(publisher, subscriber, warmup=1, samples=3)


@pytest.mark.parametrize('owner,row', [
    ('publisher', [1, 106, 105, 111]),
    ('publisher', [1, 100, 112, 111]),
    ('subscriber', [1, 119, 118]),
    ('subscriber', [1, 104, 118]),
])
def test_timestamp_ordering_is_validated(owner, row):
    publisher, subscriber = records()
    target = publisher if owner == 'publisher' else subscriber
    target['samples'][1] = row
    with pytest.raises(ValueError, match='timestamp'):
        analyze(publisher, subscriber, warmup=1, samples=3)


def test_invalid_warmup_timestamps_are_not_hidden_by_exclusion():
    publisher, subscriber = records()
    subscriber['samples'][0] = [0, 0, 1]
    with pytest.raises(ValueError, match='timestamp'):
        analyze(publisher, subscriber, warmup=1, samples=3)


@pytest.mark.parametrize('invalid', [None, [], {}, {'samples': None},
                                     {'samples': ['invalid']},
                                     {'samples': [[0, 1]]}])
@pytest.mark.parametrize('owner', ['publisher', 'subscriber'])
def test_malformed_records_raise_value_error(owner, invalid):
    publisher, subscriber = records()
    if owner == 'publisher':
        publisher = invalid
    else:
        subscriber = invalid
    with pytest.raises(ValueError, match=owner):
        analyze(publisher, subscriber, warmup=1, samples=3)


def test_analysis_does_not_modify_input_evidence():
    publisher, subscriber = records()
    before = deepcopy((publisher, subscriber))
    analyze(publisher, subscriber, warmup=1, samples=3)
    assert (publisher, subscriber) == before


@pytest.mark.parametrize('proposed', ['hermos', 'herm0s', 'agnocast'])
def test_report_compares_both_latency_boundaries_and_tails_with_one_sign(tmp_path, proposed):
    from experiments.g5.cpu_latency.run import report
    results = []
    for transport, callback, fill in [('dds', 1000, 2000), (proposed, 500, 3000)]:
        results.append(dict(
            case=dict(layout='dynamic', size=1024, rmw='test-rmw', transport=transport),
            complete=True, received=10, expected=10,
            publish_to_callback_ns=dict(mean=callback, p99=2 * callback),
            fill_to_consumer_ns=dict(mean=fill, p99=2 * fill),
            publish_call_ns=dict(mean=200)))
    report(results, tmp_path, {})
    text = (tmp_path / 'report.md').read_text()
    assert 'Positive percentages mean additional latency' in text
    assert 'Publish→callback mean' in text and 'Fill→consumer p99' in text
    assert '| -50.00% | -50.00% | +50.00% | +50.00% |' in text
    assert 'Publish-call mean' in text
    assert f'| {proposed} |' in text


@pytest.mark.parametrize('baseline_mean,complete', [(0, True), (1, False)])
def test_report_invalidates_zero_baseline_and_loss_without_dividing(
        tmp_path, baseline_mean, complete):
    from experiments.g5.cpu_latency.run import report
    results = []
    for transport in ('dds', 'hermos'):
        stats = None if not complete else dict(mean=baseline_mean, p99=baseline_mean)
        results.append(dict(
            case=dict(layout='dynamic', size=1024, rmw='test-rmw', transport=transport),
            complete=complete, received=int(complete), expected=1,
            publish_to_callback_ns=stats, fill_to_consumer_ns=stats,
            publish_call_ns=dict(mean=200)))
    report(results, tmp_path, {})
    text = (tmp_path / 'report.md').read_text()
    assert 'INVALID' in text
    if not complete:
        assert 'N/A' in text
