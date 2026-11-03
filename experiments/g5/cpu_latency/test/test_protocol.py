"""Measurement settings must be fixed and malformed plans must fail closed."""

import pytest

from experiments.g5.cpu_latency.protocol import cases, validate
from experiments.g5.harness import verify_teardown


def protocol(**changes):
    value = dict(sizes=[1024, 1048576], layouts=['static', 'dynamic'],
                 rmws=['rmw_cyclonedds_cpp', 'rmw_fastrtps_cpp'],
                 transports=['dds', 'hermos'], samples=1000, warmup=100,
                 period_ns=10000000, depth=4, full_check=False)
    value.update(changes)
    return value


def test_comparison_matrix_contains_matched_static_dynamic_and_both_rmws():
    values = cases(protocol())
    assert len(values) == 16
    assert len({item['name'] for item in values}) == 16
    assert values[0]['type'] == 'Fixed1K'
    assert {v['type'] for v in values} == {'Fixed1K', 'Fixed1M', 'Dynamic'}


@pytest.mark.parametrize('changes', [
    {'sizes': [1024, 1024]}, {'sizes': [1234]}, {'samples': True},
    {'warmup': -1}, {'period_ns': 0}, {'layouts': []},
    {'rmws': ['unknown']}, {'transports': ['udp']}, {'full_check': 'yes'},
    {'resource_diagnostics': 'yes'}, {'resource_diagnostics': 1},
])
def test_invalid_measurement_contract_is_rejected(changes):
    with pytest.raises(ValueError):
        validate(protocol(**changes))


def test_empty_peer_exit_codes_cannot_certify_success():
    record = dict(verified_teardown=True, teardown_complete=True, echild=True,
                  peer_failed=False, returncodes=[], descendants=[])
    with pytest.raises(RuntimeError):
        verify_teardown(record, peers=2)


@pytest.mark.parametrize('field,value', [
    ('returncodes', [0, 70]), ('echild', False), ('descendants', [{'returncode': 1}])])
def test_peer_and_descendant_failures_cannot_be_hidden_by_teardown(field, value):
    record = dict(verified_teardown=True, teardown_complete=True, echild=True,
                  peer_failed=False, returncodes=[0, 0], descendants=[])
    record[field] = value
    with pytest.raises(RuntimeError):
        verify_teardown(record, peers=2)


def cpu_result(transport, repeat, mean):
    return dict(case=dict(layout='dynamic', size=1024, rmw='test-rmw',
                          transport=transport, repeat=repeat),
                expected=2, received=2, complete=True,
                publish_to_callback_ns=dict(mean=mean, p99=mean),
                fill_to_consumer_ns=dict(mean=mean, p99=mean),
                publish_call_ns=dict(mean=mean))


def test_cpu_report_never_matches_a_baseline_from_another_repeat(tmp_path):
    from experiments.g5.cpu_latency.run import report
    report([cpu_result('dds', 1, 20000), cpu_result('hermos', 2, 10000)], tmp_path, {})
    text = (tmp_path / 'report.md').read_text()
    assert '-50.00%' not in text
    assert '| Repeat |' in text


def test_cpu_report_keeps_each_repeat_comparison_instead_of_pooling(tmp_path):
    from experiments.g5.cpu_latency.run import report
    report([cpu_result('dds', 1, 20000), cpu_result('hermos', 1, 10000),
            cpu_result('dds', 2, 10000), cpu_result('hermos', 2, 30000)], tmp_path, {})
    text = (tmp_path / 'report.md').read_text()
    assert '| 1 | dynamic | 1024 | test-rmw | -50.00%' in text
    assert '| 2 | dynamic | 1024 | test-rmw | +200.00%' in text


@pytest.mark.parametrize('diagnostic', [False, True])
def test_cpu_cli_records_repeat_contract_without_reshuffling_pairs(tmp_path, monkeypatch, diagnostic):
    from experiments.g5.cpu_latency import run
    import sys
    monkeypatch.delenv('LD_PRELOAD', raising=False)
    argv = ['cpu-run', '--output', str(tmp_path), '--repeats', '3']
    monkeypatch.setattr(sys, 'argv', argv + (['--resource-diagnostics'] if diagnostic else []))
    captured = []
    monkeypatch.setattr(run, 'run_matrix', lambda *args, **kwargs: captured.append((args, kwargs)))
    run.main()
    (output, settings, matrix), options = captured[0]
    assert output == tmp_path and settings['repeats'] == 3
    assert settings['resource_diagnostics'] is diagnostic
    assert settings['case_order'] == 'repeat-major-seeded-pair-groups'
    assert settings['pair_transport_order'] == 'dds-first-odd-hermos-first-even'
    assert options['shuffle'] is False
    for first, second in zip(matrix[::2], matrix[1::2]):
        assert (first['size'], first['layout'], first['rmw'], first['repeat']) == (
            second['size'], second['layout'], second['rmw'], second['repeat'])


@pytest.mark.parametrize('value', ['0', '-1', 'bad'])
def test_cpu_cli_rejects_invalid_repeat_count(tmp_path, monkeypatch, value):
    from experiments.g5.cpu_latency import run
    import sys
    monkeypatch.setattr(sys, 'argv', ['cpu-run', '--output', str(tmp_path), '--repeats', value])
    with pytest.raises(SystemExit) as error:
        run.main()
    assert error.value.code == 2
