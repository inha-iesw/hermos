"""Linux counter parsing must not attribute other processes' sockets to peers."""

import pytest

from experiments.g5.cpu_latency.observation import udp_counters, udp_sockets


def test_udp_counter_names_are_joined_with_matching_values():
    text = ('Ip: Forwarding InReceives\nIp: 1 900\n'
            'Udp: InDatagrams InErrors RcvbufErrors\nUdp: 50 7 6\n')
    assert udp_counters(text) == {'InDatagrams': 50, 'InErrors': 7, 'RcvbufErrors': 6}


@pytest.mark.parametrize('text', ['', 'Udp: InErrors\n',
                                  'Udp: InErrors RcvbufErrors\nUdp: 1\n'])
def test_missing_or_truncated_udp_counters_fail_instead_of_reporting_zero(text):
    with pytest.raises(ValueError):
        udp_counters(text)


def test_socket_counters_include_only_owned_inodes_and_decode_queues():
    text = ('sl local_address rem_address st tx_queue rx_queue tr tm->when '
            'retrnsmt uid timeout inode ref pointer drops\n'
            '0: 0100007F:1234 00000000:0000 07 00000010:00000020 '
            '00:00000000 00000000 1000 0 701 2 0000000000000000 3\n'
            '1: 00000000:4321 00000000:0000 07 00000000:00000000 '
            '00:00000000 00000000 1000 0 702 2 0000000000000000 99\n')
    assert udp_sockets(text, {'701'}) == [
        dict(inode=701, local_address='0100007F:1234', remote_address='00000000:0000',
             tx_queue_bytes=16, rx_queue_bytes=32, drops=3)]


def test_unsupported_proc_udp_layout_fails_explicitly():
    with pytest.raises(ValueError):
        udp_sockets('sl local_address inode\n', set())
