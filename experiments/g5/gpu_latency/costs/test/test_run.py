"""The cost runner is explicitly diagnostic and cannot silently use the main peer."""

import json

import pytest

from experiments.g5.gpu_latency.costs import run


def test_default_cost_matrix_is_small_and_every_config_is_ineligible_for_headline_timing(
        tmp_path, monkeypatch):
    captured = []
    monkeypatch.delenv('LD_PRELOAD', raising=False)
    monkeypatch.setattr(run, 'run_matrix', lambda *args, **kwargs: captured.append((args, kwargs)))
    run.main(['--output', str(tmp_path)])
    (_, protocol, matrix), options = captured[0]
    assert protocol['timing_diagnostics'] is True and protocol['headline_eligible'] is False
    assert len(matrix) == 8
    assert {case['size'] for case in matrix} == {1024, 4194304}
    assert {case['descriptor_transport'] for case in matrix} == {'dds', 'hermos'}
    assert all(case['timing_diagnostics'] is True and case['headline_eligible'] is False for case in matrix)
    assert options['capture'] is run.base.identity


def test_cost_runner_selects_requested_sizes_rmw_and_transport(tmp_path, monkeypatch):
    captured = []
    monkeypatch.delenv('LD_PRELOAD', raising=False)
    monkeypatch.setattr(run, 'run_matrix', lambda *args, **kwargs: captured.append(args))
    run.main(['--output', str(tmp_path), '--sizes', '1024', '--rmws', 'rmw_fastrtps_cpp',
              '--transports', 'hermos', '--samples', '4', '--warmup', '2'])
    _, protocol, matrix = captured[0]
    assert len(matrix) == 1
    assert matrix[0]['rmw'] == 'rmw_fastrtps_cpp' and matrix[0]['descriptor_transport'] == 'hermos'
    assert protocol['samples'] == 4 and protocol['warmup'] == 2


def test_run_case_joins_cost_rows_to_actual_receipts_and_uses_cost_factory(tmp_path, monkeypatch):
    config = dict(warmup=0, samples=1, descriptor_transport='dds')
    dispatched = []

    def pair(value, path, *, peer_factory):
        dispatched.append(peer_factory)
        for role in ('publisher', 'subscriber'):
            (path / f'{role}.json').write_text(json.dumps(dict(samples=[[0]])))
            events = [dict(stage='prepare', started_ns=1, finished_ns=3, failed=False)]
            (path / f'{role}-costs.json').write_text(json.dumps(dict(
                version=1, config=value, role=role, timing_diagnostics=True,
                headline_eligible=False, samples=[dict(sequence=0, events=events)])))
        return dict(complete=True)

    monkeypatch.setattr(run.base, 'run_pair', pair)
    result = run.run_case(config, {}, tmp_path)
    assert dispatched == ['experiments.g5.gpu_latency.costs.peer:factory']
    assert result['costs']['publisher']['measured_samples'] == 1
    assert result['timing_diagnostics'] is True
    assert (tmp_path / 'costs-analysis.json').is_file()
    # Missing telemetry is not silently reported as zero measured cost.
    missing = json.loads((tmp_path / 'subscriber-costs.json').read_text())
    missing['samples'] = []
    (tmp_path / 'subscriber-costs.json').write_text(json.dumps(missing))
    monkeypatch.setattr(run.base, 'run_pair', lambda *args, **kwargs: dict(complete=True))
    with pytest.raises(ValueError, match='sequence'):
        run.run_case(config, {}, tmp_path)
