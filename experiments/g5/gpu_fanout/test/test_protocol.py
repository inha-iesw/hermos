"""Keep fanout/load pairs complete, adjacent and independent across repeats."""

import pytest

from experiments.g5.gpu_fanout.run import matrix, protocol, peer_command


def test_default_matrix_is_twelve_adjacent_rate_matched_conditions():
    settings = protocol()
    cases = matrix(settings)
    assert len(cases) == len({case['name'] for case in cases}) == 12
    assert {case['subscribers'] for case in cases} == {8}
    assert {case['size'] for case in cases} == {24883200}
    assert {case['rate_hz'] for case in cases} == {5, 30}
    for first, second in zip(cases[::2], cases[1::2]):
        assert (first['rate_hz'], first['subscribers'], first['repeat']) == (
            second['rate_hz'], second['subscribers'], second['repeat'])
        expected = ['vanilla', 'hermos'] if first['repeat'] % 2 else ['hermos', 'vanilla']
        assert [first['transport'], second['transport']] == expected
    assert matrix(settings) == cases


def test_n1_control_remains_distinct_from_n8_and_rate_groups():
    cases = matrix(protocol(subscribers=(1, 8)))
    assert len(cases) == len({case['name'] for case in cases}) == 24
    assert len({(c['subscribers'], c['rate_hz'], c['repeat']) for c in cases}) == 12


def test_small_message_control_is_a_separate_single_size_matrix():
    large = matrix(protocol(rates=(5,)))
    small = matrix(protocol(size=65536, rates=(5,)))
    assert len(small) == 6 and {case['size'] for case in small} == {65536}
    assert not {case['name'] for case in small} & {case['name'] for case in large}
    assert {case['rate_hz'] for case in small} == {5}


@pytest.mark.parametrize('options', [
    {'subscribers': ()}, {'subscribers': (0,)}, {'subscribers': (True,)},
    {'subscribers': (8, 8)}, {'rates': (0,)}, {'rates': (5, 5)},
    {'samples': 0}, {'warmup': -1}, {'repeats': 0}, {'samples': True},
    {'size': 0}, {'size': True}, {'size': 1024},
])
def test_invalid_protocol_is_rejected_before_any_execution(options):
    with pytest.raises(ValueError):
        protocol(**options)


def test_only_hermos_uses_gpu_launcher_and_both_peers_keep_same_config(tmp_path):
    cases = matrix(protocol(rates=(5,), repeats=1))
    config = tmp_path / 'config.json'
    output = tmp_path / 'subscriber-1.json'
    vanilla, hermos = [peer_command(case, 'subscriber', config, output) for case in cases]
    assert 'rclpy_hermos_gpu.launch' not in vanilla
    assert 'experiments.g5.gpu_fanout.vanilla_peer' in vanilla
    assert 'rclpy_hermos_gpu.launch' in hermos
    assert 'experiments.g5.gpu_latency.peer:factory' in hermos
    assert str(config) in vanilla and str(config) in hermos
    assert str(output) in vanilla and str(output) in hermos
