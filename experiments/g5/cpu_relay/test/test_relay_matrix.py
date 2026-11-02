"""The explicit relay comparison must include its matched control and all cases."""

import pytest

from experiments.g5.cpu_relay.run import cases, report


def test_matrix_has_three_paths_and_both_rmws_at_each_size():
    matrix = cases()
    assert len(matrix) == len({case['name'] for case in matrix}) == 18
    assert {case['size'] for case in matrix} == {1024, 65536, 1048576}
    assert {case['rmw'] for case in matrix} == {'rmw_cyclonedds_cpp', 'rmw_fastrtps_cpp'}
    assert {case['path'] for case in matrix} == {
        'dds-to-dds', 'hermos-to-dds', 'dds-to-hermos'}
    assert all(case['layout'] == 'dynamic' and case['type'] == 'Dynamic' for case in matrix)
    for size in (1024, 65536, 1048576):
        assert sum(case['size'] == size for case in matrix) == 6


def test_report_never_computes_overhead_from_an_incomplete_control(tmp_path):
    matrix = cases()
    results = []
    for case in matrix[:2]:
        results.append(dict(
            case=case, complete=case['path'] != 'dds-to-dds', missing_relay_records=0,
            source_to_sink=dict(received=9, expected=10,
                                publish_to_callback_ns=dict(mean=100, p99=120))))
    report(results, tmp_path)
    text = (tmp_path / 'report.md').read_text()
    assert 'INVALID (incomplete delivery or relay evidence)' in text
    assert '+0.00%' not in text


def test_declared_sizes_and_rmws_drive_the_relay_matrix():
    protocol = dict(sizes=[65536], layouts=['dynamic'], rmws=['rmw_cyclonedds_cpp'],
                    transports=['dds', 'hermos'], samples=2, warmup=1,
                    period_ns=1000000, depth=4, full_check=True)
    matrix = cases(protocol)
    assert len(matrix) == 3
    assert {case['size'] for case in matrix} == {65536}
    assert {case['rmw'] for case in matrix} == {'rmw_cyclonedds_cpp'}


@pytest.mark.parametrize('changes', [
    {'layouts': ['static']}, {'layouts': ['dynamic', 'static']},
    {'transports': ['dds']}, {'transports': ['hermos']},
])
def test_fixed_relay_paths_reject_misleading_protocol_fields(changes):
    protocol = dict(sizes=[65536], layouts=['dynamic'], rmws=['rmw_cyclonedds_cpp'],
                    transports=['dds', 'hermos'], samples=2, warmup=1,
                    period_ns=1000000, depth=4, full_check=True)
    protocol.update(changes)
    with pytest.raises(ValueError, match='relay requires'):
        cases(protocol)


def test_relay_transport_order_does_not_change_fixed_paths():
    protocol = dict(sizes=[65536], layouts=['dynamic'], rmws=['rmw_cyclonedds_cpp'],
                    transports=['hermos', 'dds'], samples=2, warmup=1,
                    period_ns=1000000, depth=4, full_check=True)
    assert {case['path'] for case in cases(protocol)} == {
        'dds-to-dds', 'hermos-to-dds', 'dds-to-hermos'}
