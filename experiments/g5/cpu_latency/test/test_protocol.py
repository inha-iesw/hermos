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
