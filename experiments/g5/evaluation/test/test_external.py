"""Independent fanout receipt and stage arithmetic oracles for imported evidence."""

import hashlib
import json

import pytest

from experiments.g5.evaluation import external


def publisher(count=3):
    return dict(time_namespace='time:[1]',
                samples=[[i, i * 1000, i * 1000 + 10, i * 1000 + 100]
                         for i in range(count)])


def subscriber(ids, callback, done):
    return dict(time_namespace='time:[1]',
                samples=[[i, i * 1000 + callback, i * 1000 + done] for i in ids])


def test_aggregate_loss_does_not_hide_the_worst_subscriber_or_all_reader_barrier():
    result = external.audit_case(publisher(), [subscriber(range(3), 15, 20),
                                               subscriber([0], 110, 120)],
                                 warmup=0, samples=3, period_ns=100)
    assert result['received'] == [3, 1]
    assert result['aggregate_lost_fraction'] == pytest.approx(1 / 3)
    assert result['worst_subscriber_lost_fraction'] == pytest.approx(2 / 3)
    assert result['all_subscribers_received_ids'] == [0]
    assert result['all_subscribers_completion_ns']['p50'] == 120
    assert result['median_subscriber_p50_ns'] == 70
    assert result['all_subscribers_within_period'] == 0
    assert result['all_subscribers_period_unsatisfied'] == 3


def test_median_stage_sum_is_not_median_e2e_but_equal_subscriber_means_are_additive():
    result = external.audit_case(publisher(), [subscriber(range(3), 11, 111),
                                               subscriber(range(3), 110, 111),
                                               subscriber(range(3), 11, 12)],
                                 warmup=0, samples=3, period_ns=200)
    assert result['stage_medians_ns'] == {
        'fill': 10, 'in_publish': 1, 'after_publish': 0, 'consume': 1}
    assert result['sum_of_stage_medians_ns'] == 12
    assert result['median_subscriber_e2e_means_ns'] == 111
    assert result['equal_subscriber_mean_e2e_ns'] == 78
    assert sum(result['equal_subscriber_mean_stages_ns'].values()) == pytest.approx(78)
    # Callback before publish return clips the overlap instead of adding the full call.
    assert result['per_subscriber'][0]['stages_ns']['in_publish']['mean'] == 1
    assert result['per_subscriber'][0]['stages_ns']['after_publish']['mean'] == 0


def test_no_receipts_or_no_common_ids_stay_explicitly_unavailable():
    result = external.audit_case(publisher(), [subscriber(range(3), 15, 20),
                                               subscriber([], 15, 20)],
                                 warmup=0, samples=3, period_ns=100)
    assert result['received'] == [3, 0]
    assert result['per_subscriber'][1]['fill_to_consumer_ns'] is None
    assert result['all_subscribers_received_ids'] == []
    assert result['all_subscribers_completion_ns'] is None
    assert result['all_subscribers_within_period'] is None
    assert result['median_subscriber_e2e_means_ns'] is None
    assert result['equal_subscriber_mean_stages_ns'] is None


def test_clock_mismatch_and_invalid_timestamp_rows_are_rejected():
    wrong_clock = subscriber(range(3), 15, 20)
    wrong_clock['time_namespace'] = 'time:[2]'
    with pytest.raises(ValueError, match='clock'):
        external.audit_case(publisher(), [wrong_clock], warmup=0, samples=3, period_ns=100)
    duplicate = subscriber([0, 0], 15, 20)
    with pytest.raises(ValueError, match='duplicate'):
        external.audit_case(publisher(), [duplicate], warmup=0, samples=3, period_ns=100)


def test_html_data_is_json_only_and_trailing_javascript_is_not_executed(tmp_path):
    marker = tmp_path / 'must-not-exist'
    html = '<script>const DATA = {"fanout": []}; throw "do not execute";</script>'
    assert external.decode_html(html) == {'fanout': []}
    assert not marker.exists()
    with pytest.raises(ValueError):
        external.decode_html('const DATA = (() => ({fanout: []}))();')


def test_html_rounding_tolerance_and_received_vector_comparison():
    value = dict(case=dict(scenario='cpu-py', variant='rclpy-vanilla',
                           size=1024, subscribers=2), received=[3, 1],
                 median_subscriber_p50_ns=1_234_000)
    html = dict(scenario='cpu-py', variant='rclpy-vanilla', size=1024, n=2,
                received=[3, 1], e2e_p50_ms=1.23)
    assert external.compare_html([html], [value])['matches'] == 1
    html['received'] = [3, 2]
    comparison = external.compare_html([html], [value])
    assert comparison['matches'] == 0
    assert comparison['cases'][0]['received_match'] is False


def test_cli_hashes_raw_inputs_writes_only_decoded_evidence_and_requires_fresh_output(tmp_path):
    root = tmp_path / 'raw'
    case = dict(name='cpu-case', scenario='cpu-py', variant='rclpy-vanilla',
                size=1024, subscribers=1)
    directory = root / case['name']
    directory.mkdir(parents=True)
    records = {
        root / 'protocol.json': dict(protocol=dict(warmup=0, samples=3, period_ns=100),
                                     cases=[case]),
        directory / 'publisher.json': publisher(),
        directory / 'subscriber-1.json': subscriber(range(3), 15, 20),
        directory / 'teardown.json': dict(verified_teardown=True, teardown_complete=True,
                                          echild=True, peer_failed=False, returncodes=[0, 0],
                                          descendants=[]),
    }
    for path, value in records.items():
        path.write_text(json.dumps(value))
    html_path = tmp_path / 'external.html'
    html_path.write_text('const DATA = ' + json.dumps(dict(fanout=[dict(
        scenario='cpu-py', variant='rclpy-vanilla', size=1024, n=1,
        received=[3], e2e_p50_ms=0.0)])) + ';')
    output = tmp_path / 'audit'
    external.main(['--html', str(html_path), '--fanout-results', str(root), '--output', str(output)])
    assert {path.name for path in output.iterdir()} == {'html-data.json', 'audit.json'}
    audit = json.loads((output / 'audit.json').read_text())
    assert audit['input_sha256'][str(html_path.resolve())] == hashlib.sha256(html_path.read_bytes()).hexdigest()
    assert all(str(path.resolve()) in audit['input_sha256'] for path in records)
    assert audit['counts']['vanilla']['cases'] == 1
    assert audit['html_comparison']['matches'] == 1
    with pytest.raises(FileExistsError):
        external.main(['--html', str(html_path), '--fanout-results', str(root), '--output', str(output)])


def test_invalid_teardown_cannot_be_promoted_to_a_valid_audit(tmp_path):
    case = dict(name='broken', scenario='cpu-py', variant='rclpy-hermos',
                size=1024, subscribers=1)
    directory = tmp_path / 'broken'
    directory.mkdir()
    (tmp_path / 'protocol.json').write_text(json.dumps(dict(
        protocol=dict(warmup=0, samples=3, period_ns=100), cases=[case])))
    (directory / 'publisher.json').write_text(json.dumps(publisher()))
    (directory / 'subscriber-1.json').write_text(json.dumps(subscriber(range(3), 15, 20)))
    (directory / 'teardown.json').write_text('{}')
    with pytest.raises(RuntimeError, match='teardown'):
        external.load_fanout(tmp_path)
