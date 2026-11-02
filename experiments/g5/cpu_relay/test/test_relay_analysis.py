"""Independent chain-evidence and three-peer termination oracles."""

import pytest

from experiments.g5.cpu_relay.analysis import analyze_chain
from experiments.g5.harness import verify_teardown


def records():
    return (
        {'samples': [[0, 0, 10, 11], [1, 100, 110, 111], [2, 200, 210, 211]]},
        {'samples': [[0, 20, 30, 31], [1, 120, 135, 136], [2, 225, 250, 251]]},
        {'samples': [[0, 40, 45], [1, 150, 155], [2, 275, 280]]},
    )


def test_chain_distinguishes_end_to_end_input_hop_and_forwarding_cost():
    result = analyze_chain(*records(), warmup=1, samples=2)
    assert result['complete'] is True
    assert result['missing_relay_records'] == 0
    assert result['source_to_sink']['publish_to_callback_ns']['mean'] == 52.5
    assert result['source_to_relay']['publish_to_callback_ns']['mean'] == 12.5
    assert result['relay_to_sink']['publish_to_callback_ns']['mean'] == 20
    assert result['source_to_relay']['source_fill_to_relay_return_ns']['mean'] == 43.5
    assert result['relay_to_sink']['relay_callback_to_sink_done_ns']['mean'] == 45
    assert 'fill_to_consumer_ns' not in result['source_to_relay']
    assert 'fill_to_consumer_ns' not in result['relay_to_sink']


def test_missing_relay_and_sink_receipt_invalidates_the_comparison():
    source, relay, sink = records()
    relay['samples'].pop()
    sink['samples'].pop()
    result = analyze_chain(source, relay, sink, warmup=1, samples=2)
    assert result['complete'] is False
    assert result['missing_relay_records'] == 1
    assert result['source_to_sink']['lost'] == 1
    assert result['source_to_relay']['lost'] == 1
    assert result['relay_to_sink'] is None


def test_sink_receipt_without_relay_record_is_impossible_evidence():
    source, relay, sink = records()
    relay['samples'].pop()
    with pytest.raises(ValueError, match='sink sequence.*relay'):
        analyze_chain(source, relay, sink, warmup=1, samples=2)


def test_sink_timestamp_before_relay_publish_is_rejected_even_with_another_gap():
    source, relay, sink = records()
    relay['samples'].pop()
    sink['samples'].pop()
    sink['samples'][1][1] = 134
    with pytest.raises(ValueError, match='precedes relay publish'):
        analyze_chain(source, relay, sink, warmup=1, samples=2)


def test_duplicate_relay_sequence_is_rejected():
    source, relay, sink = records()
    relay['samples'].append(relay['samples'][1][:])
    with pytest.raises(ValueError, match='duplicate'):
        analyze_chain(source, relay, sink, warmup=1, samples=2)


def test_missing_warmup_relay_record_is_reported_and_not_hidden():
    source, relay, sink = records()
    relay['samples'].pop(0)
    sink['samples'].pop(0)
    result = analyze_chain(source, relay, sink, warmup=1, samples=2)
    assert result['source_to_sink']['complete'] is True
    assert result['missing_relay_records'] == 1
    assert result['complete'] is False


def test_total_loss_preserves_counts_without_fabricating_latency_statistics():
    source, _, _ = records()
    result = analyze_chain(source, {'samples': []}, {'samples': []}, warmup=1, samples=2)
    assert result['complete'] is False
    delivery = result['source_to_sink']
    assert (delivery['expected'], delivery['received'], delivery['lost']) == (2, 0, 2)
    assert delivery['complete'] is False
    assert delivery['publish_to_callback_ns'] is None
    assert delivery['fill_to_consumer_ns'] is None
    assert delivery['publish_call_ns']['mean'] == 1
    assert result['missing_relay_records'] == 3


@pytest.mark.parametrize('row', [
    [1, 120, 119, 136], [1, 120, 135, 134], [1, 120, '135', 136],
    [1, 120, 135], [True, 120, 135, 136],
])
def test_invalid_relay_timestamp_records_cannot_be_hidden_by_projection(row):
    source, relay, sink = records()
    relay['samples'][1] = row
    with pytest.raises(ValueError, match='relay'):
        analyze_chain(source, relay, sink, warmup=1, samples=2)


def teardown(**changes):
    record = dict(verified_teardown=True, teardown_complete=True, echild=True,
                  peer_failed=False, returncodes=[0, 0, 0], descendants=[{'returncode': 0}])
    record.update(changes)
    return record


def test_exactly_three_successful_peers_and_natural_descendant_exit_are_required():
    verify_teardown(teardown(), peers=3)


@pytest.mark.parametrize('changes', [
    {'returncodes': []}, {'returncodes': [0, 0]}, {'returncodes': [0, 0, 0, 0]},
    {'returncodes': [0, 70, 0]}, {'returncodes': [0, -9, 0]},
    {'verified_teardown': False}, {'teardown_complete': False}, {'echild': False},
    {'peer_failed': True}, {'descendants': [{'returncode': 1}]},
    {'returncodes': [False, 0, 0]},
])
def test_incomplete_or_failed_chain_teardown_is_rejected(changes):
    with pytest.raises(RuntimeError, match='3-peer'):
        verify_teardown(teardown(**changes), peers=3)
