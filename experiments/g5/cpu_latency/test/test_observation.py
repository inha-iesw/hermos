"""Linux counter parsing must not attribute other processes' sockets to peers."""

import pytest

from experiments.g5.cpu_latency.observation import udp_counters, udp_sockets
from experiments.g5.cpu_latency import observation


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


def test_smaps_separates_process_and_shared_heap_residency():
    text = ('1000-2000 rw-p 00000000 00:00 0 [heap]\nRss: 12 kB\nPss: 12 kB\n'
            '2000-3000 rw-s 00000000 00:01 1 /dev/shm/hermos@12\n'
            'Rss: 8 kB\nPss: 4 kB\n'
            '3000-4000 r--s 00000000 00:01 2 /dev/shm/hermos@34 (deleted)\n'
            'Rss: 16 kB\nPss: 8 kB\n'
            '4000-5000 rw-s 00000000 00:01 3 /dev/shm/other\n'
            'Rss: 4 kB\nPss: 2 kB\n')
    assert observation.smaps_memory(text) == {
        'process': {'rss_kib': 40, 'pss_kib': 26},
        'shared_heap': {'mapping_count': 2, 'rss_kib': 24, 'pss_kib': 12}}


def test_smaps_absent_pss_is_unavailable_instead_of_zero():
    text = '1000-2000 rw-s 00000000 00:01 1 /dev/shm/hermos@12\nRss: 8 kB\n'
    result = observation.smaps_memory(text)
    assert result['process'] == {'rss_kib': 8, 'pss_kib': None}
    assert result['shared_heap'] == {'mapping_count': 1, 'rss_kib': 8, 'pss_kib': None}


def test_smaps_without_shared_heap_reports_observed_zero_shared_residency():
    result = observation.smaps_memory(
        '1000-2000 rw-p 00000000 00:00 0 [heap]\nRss: 8 kB\nPss: 8 kB\n')
    assert result['shared_heap'] == {'mapping_count': 0, 'rss_kib': 0, 'pss_kib': 0}


def test_thread_snapshot_keeps_each_workers_own_affinity_and_scheduler(tmp_path, monkeypatch):
    for tid, name in ((101, 'main'), (102, 'worker')):
        directory = tmp_path / str(tid)
        directory.mkdir()
        (directory / 'comm').write_text(name + '\n')
    monkeypatch.setattr(observation, 'Path', lambda _: tmp_path)
    monkeypatch.setattr(observation.os, 'sched_getaffinity', lambda tid: {2} if tid == 101 else {3, 4})
    monkeypatch.setattr(observation.os, 'sched_getscheduler', lambda tid: 0 if tid == 101 else 1)
    monkeypatch.setattr(observation.os, 'sched_getparam', lambda tid: type(
        'Param', (), {'sched_priority': 0 if tid == 101 else 10})())
    assert observation.thread_snapshot() == [
        {'tid': 101, 'name': 'main', 'cpu_affinity': [2], 'scheduler': 0, 'priority': 0},
        {'tid': 102, 'name': 'worker', 'cpu_affinity': [3, 4], 'scheduler': 1, 'priority': 10}]


def test_cpu_frequency_missing_policy_fields_are_explicit(tmp_path, monkeypatch):
    policy = tmp_path / 'policy0'
    policy.mkdir()
    (policy / 'related_cpus').write_text('0 1\n')
    (policy / 'scaling_governor').write_text('schedutil\n')
    (policy / 'scaling_cur_freq').write_text('1200000\n')
    monkeypatch.setattr(observation, 'Path', lambda _: tmp_path)
    result = observation.cpu_frequency_snapshot()
    assert result == [{'policy': 'policy0', 'related_cpus': '0 1',
                       'scaling_governor': 'schedutil', 'scaling_cur_freq_khz': 1200000,
                       'scaling_min_freq_khz': None, 'scaling_max_freq_khz': None}]


def test_unavailable_smaps_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(observation, 'Path', lambda _: tmp_path / 'missing')
    result = observation.memory_snapshot()
    assert result['smaps'] is None
    assert result['smaps_error']
