"""Bound the comparison matrix and prevent receipt loss from becoming a gain claim."""

import itertools
import json

import pytest

from experiments.g5.gpu_latency import run as run_module
from experiments.g5.gpu_latency.run import cases, report


def test_comparison_matrix_has_each_transport_for_every_size_and_rmw():
    matrix = cases(True)
    expected = set(itertools.product(
        (1024, 4096, 65536, 1048576, 4194304),
        ('rmw_cyclonedds_cpp', 'rmw_fastrtps_cpp'), ('dds', 'hermos')))
    assert len(matrix) == 20
    assert len({case['name'] for case in matrix}) == 20
    assert {(case['size'], case['rmw'], case['descriptor_transport'])
            for case in matrix} == expected


def test_original_matrix_keeps_five_hermos_cyclone_cases():
    matrix = cases(False)
    assert len(matrix) == 5
    assert [case['size'] for case in matrix] == [1024, 4096, 65536, 1048576, 4194304]
    assert all(case['rmw'] == 'rmw_cyclonedds_cpp' and
               case['descriptor_transport'] == 'hermos' and
               case['name'] == str(case['size']) for case in matrix)


def test_report_marks_incomplete_comparison_invalid(tmp_path):
    results = [dict(
        case=dict(size=1024, rmw='rmw_cyclonedds_cpp', descriptor_transport=transport),
        expected=1000, received=received, complete=received == 1000,
        publish_to_callback_ns=dict(mean=100),
        publish_call_ns=dict(mean=50),
        fill_to_gpu_done_ns=dict(mean=mean + 100, p99=mean * 2 + 100),
        publish_to_gpu_done_ns=dict(mean=mean, p99=mean * 2))
        for transport, received, mean in [('dds', 999, 200), ('hermos', 1000, 100)]]
    report(results, tmp_path)
    text = (tmp_path / 'report.md').read_text()
    assert '999/1000' in text
    assert '1024 bytes, rmw_cyclonedds_cpp: INVALID (receipt loss)' in text
    assert '-50.00%' not in text


@pytest.fixture
def protocol():
    return dict(dtype='uint8', blocks=16, max_reads=16, depth=16, descriptor_bytes=4096,
                retained_bytes_per_read=4096, warmup=64, samples=1000, period_ns=20000000)


def test_make_config_propagates_nondefault_protocol_and_derives_retention_budget(protocol):
    protocol.update(blocks=7, max_reads=3, depth=5, descriptor_bytes=2048,
                    retained_bytes_per_read=8192, warmup=0, samples=9, period_ns=11000000)
    case = dict(size=65536, rmw='rmw_fastrtps_cpp', descriptor_transport='dds', name='case')
    config = run_module.make_config(case, protocol)
    assert config['blocks'] == 7 and config['max_reads'] == 3
    assert config['depth'] == 5 and config['descriptor_bytes'] == 2048
    assert config['retained_bytes_per_read'] == 8192 and config['max_retained_bytes'] == 24576
    assert config['warmup'] == 0 and config['samples'] == 9 and config['period_ns'] == 11000000
    assert config['size'] == 65536 and config['rmw'] == 'rmw_fastrtps_cpp'
    assert config['descriptor_transport'] == 'dds' and config['dtype'] == 'uint8'
    assert config['topic'].startswith('/g5/p') and config['topic'].endswith('/gpu')
    assert 'max_retained_bytes' not in protocol and 'topic' not in case


@pytest.mark.parametrize('key,value', [
    (key, value)
    for key in ('blocks', 'max_reads', 'depth', 'descriptor_bytes',
                'retained_bytes_per_read', 'samples', 'period_ns')
    for value in (0, True)
] + [('warmup', -1), ('warmup', True), ('dtype', 'float32')])
def test_make_config_rejects_invalid_protocol_before_creating_peers(protocol, key, value):
    protocol[key] = value
    with pytest.raises(ValueError, match=key):
        run_module.make_config(cases(False)[0], protocol)


def test_make_config_rejects_retention_budget_smaller_than_one_descriptor(protocol):
    protocol['retained_bytes_per_read'] = 2048
    with pytest.raises(ValueError, match='must cover descriptor_bytes'):
        run_module.make_config(cases(False)[0], protocol)


@pytest.mark.parametrize('key,value', [
    ('size', 1025), ('size', True), ('rmw', 'unknown'), ('descriptor_transport', 'unknown')])
def test_make_config_rejects_unsupported_case(protocol, key, value):
    case = cases(False)[0]
    case[key] = value
    with pytest.raises(ValueError, match='invalid GPU case'):
        run_module.make_config(case, protocol)


@pytest.mark.parametrize('transport', ['dds', 'hermos'])
def test_runtime_arguments_use_protocol_budgets_without_starting_a_device_epoch(
        protocol, tmp_path, monkeypatch, transport):
    protocol.update(blocks=7, max_reads=3, samples=9, warmup=2, period_ns=11000000)
    config = run_module.make_config(
        dict(size=65536, rmw='rmw_fastrtps_cpp', descriptor_transport=transport), protocol)
    captured = []

    class StopBeforeEpoch(Exception):
        pass

    def stop(commands, path, **kwargs):
        captured.append((commands, path, kwargs))
        raise StopBeforeEpoch

    monkeypatch.setattr(run_module, 'execute_epoch', stop)
    path = tmp_path / 'case'
    with pytest.raises(StopBeforeEpoch):
        run_module.run_pair(config, path)
    commands, actual_path, options = captured[0]
    assert len(commands) == 2 and actual_path == path
    for command, expected_blocks in zip(commands, ('7', '0')):
        argv = command['argv']
        assert argv[argv.index('--blocks') + 1] == expected_blocks
        assert argv[argv.index('--max-reads') + 1] == '3'
        assert argv[argv.index('--block-bytes') + 1] == '65536'
        assert argv[argv.index('--max-message-bytes') + 1] == '65536'
        assert command['env']['RMW_IMPLEMENTATION'] == 'rmw_fastrtps_cpp'
    assert options['expected'] == 11 and options['period_ns'] == 11000000
    assert json.loads((path / 'config.json').read_text()) == config


def _report_result(transport, *, received=1000, done_mean=30000):
    return dict(
        case=dict(size=1024, rmw='rmw_cyclonedds_cpp', descriptor_transport=transport),
        expected=1000, received=received, complete=received == 1000,
        publish_to_callback_ns=dict(mean=20000), publish_call_ns=dict(mean=5000),
        fill_to_gpu_done_ns=dict(mean=40000, p99=60000),
        publish_to_gpu_done_ns=dict(mean=done_mean, p99=50000))


@pytest.mark.parametrize('proposed', ['hermos', 'herm0s', 'agnocast'])
def test_report_includes_all_distinct_timing_boundaries_and_positive_additional_delay(tmp_path, proposed):
    report([_report_result('dds', done_mean=20000), _report_result(proposed)], tmp_path)
    text = (tmp_path / 'report.md').read_text()
    assert '| 20.00 | 30.00 | 50.00 | 5.00 | 40.00 | 60.00 | True |' in text
    assert '1024 bytes, rmw_cyclonedds_cpp: +50.00%' in text
    assert 'must not be added' in text
    assert f'| {proposed} |' in text


def test_report_preserves_all_loss_case_and_marks_unmeasured_latencies_unavailable(tmp_path):
    lost = _report_result('hermos', received=0)
    for metric in ('publish_to_callback_ns', 'fill_to_gpu_done_ns', 'publish_to_gpu_done_ns'):
        lost[metric] = None
    report([_report_result('dds'), lost], tmp_path)
    text = (tmp_path / 'report.md').read_text()
    assert '| 0/1000 | N/A | N/A | N/A | 5.00 | N/A | N/A | False |' in text
    assert '1024 bytes, rmw_cyclonedds_cpp: INVALID (receipt loss)' in text


@pytest.mark.parametrize('baseline_mean', [None, 0])
def test_report_does_not_claim_gain_without_a_usable_baseline(tmp_path, baseline_mean):
    baseline = _report_result('dds', done_mean=baseline_mean)
    if baseline_mean is None:
        baseline['publish_to_gpu_done_ns'] = None
    report([baseline, _report_result('hermos')], tmp_path)
    text = (tmp_path / 'report.md').read_text()
    assert '1024 bytes, rmw_cyclonedds_cpp: INVALID (unavailable or zero baseline)' in text
