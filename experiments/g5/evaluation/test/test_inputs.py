"""The evaluator must reject inconsistent identities rather than compare labels."""

import json

import pytest

from experiments.g5.evaluation.run import load_run


@pytest.fixture
def evidence(tmp_path):
    case = dict(name='cpu', size=1024, layout='dynamic', rmw='rmw_cyclonedds_cpp',
                transport='hermos', type='Dynamic')
    data = {'protocol.json': dict(protocol=dict(warmup=0, samples=2, period_ns=10), cases=[case]),
            'cpu/publisher.json': dict(case=dict(case), time_namespace='time:[123]',
                                       samples=[[0, 100, 101, 102], [1, 110, 111, 112]]),
            'cpu/subscriber.json': dict(case=dict(case), time_namespace='time:[123]',
                                        samples=[[0, 103, 104], [1, 113, 114]]),
            'cpu/teardown.json': dict(verified_teardown=True, teardown_complete=True,
                                      echild=True, peer_failed=False, returncodes=[0, 0],
                                      descendants=[])}

    def save():
        for name, value in data.items():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value))
        return tmp_path

    return data, save


def test_valid_raw_evidence_is_hashed(evidence):
    _, save = evidence
    result = load_run(save())
    assert result['results'][0]['complete']
    assert len(result['input_sha256']) == 4


def test_cost_diagnostic_protocol_is_retained_for_comparison_exclusion(evidence):
    data, save = evidence
    data['protocol.json']['protocol']['timing_diagnostics'] = True
    result = load_run(save())
    assert result['results'][0]['timing_diagnostics'] is True


def test_manifest_transport_must_match_both_peers(evidence):
    data, save = evidence
    data['protocol.json']['cases'][0]['transport'] = 'dds'
    with pytest.raises(ValueError, match='case identity'):
        load_run(save())


def test_missing_clock_namespace_is_not_proof_of_shared_clock(evidence):
    data, save = evidence
    for name in ('publisher', 'subscriber'):
        del data[f'cpu/{name}.json']['time_namespace']
    with pytest.raises(ValueError, match='clock namespace'):
        load_run(save())


def test_duplicate_manifest_case_is_not_an_independent_repeat(evidence):
    data, save = evidence
    data['protocol.json']['cases'] *= 2
    with pytest.raises(ValueError, match='duplicate case'):
        load_run(save())


def test_unknown_transport_cannot_be_labelled_as_proposed(evidence):
    data, save = evidence
    data['protocol.json']['cases'][0]['transport'] = 'unknown'
    with pytest.raises(ValueError, match='transport'):
        load_run(save())
