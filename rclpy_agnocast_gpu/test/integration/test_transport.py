"""Separate-process Python and C++ GPU descriptor interoperability."""

import json
import signal
import time
import uuid

import pytest

from support import Protocol, peer, send_signal, successful


def test_protocol_waits_for_a_complete_output_line(tmp_path):
    path = tmp_path / 'peer-0.stdout'
    path.write_text('RECEIVED')

    class Peer:
        epoch_dir = tmp_path

        def poll(self):
            # Unbuffered print() may write its fields and newline separately.
            with path.open('a') as output:
                output.write(' {"sum": 48.0}\n')
            return None

    assert Protocol(Peer()).receive(0) == 'RECEIVED {"sum": 48.0}'


def test_successful_teardown_requires_a_peer_exit_code():
    record = dict(verified_teardown=True, teardown_complete=True, echild=True,
                  peer_failed=False, returncodes=[], descendants=[])
    with pytest.raises(AssertionError):
        successful(record)


@pytest.mark.parametrize('producer,consumer,dtype', [
    ('python', 'python', 'uint8'), ('python', 'python', 'float32'),
    ('python', 'cpp', 'uint8'), ('cpp', 'python', 'uint8')])
def test_tensor_transport(epochs, producer, consumer, dtype):
    topic = '/g4_tensor_' + uuid.uuid4().hex
    epoch = epochs([peer(producer, 'publisher', topic, dtype),
                    peer(consumer, 'subscriber', topic, dtype)])
    protocol = Protocol(epoch)
    assert protocol.receive(0).startswith('READY')
    assert protocol.receive(1).startswith('READY')
    # Registration is complete before either process starts its executor.
    elements = 64 if dtype == 'uint8' else 16
    for value in (3, 17, 91):
        protocol.publish(value)
        if consumer == 'cpp':
            assert protocol.ask(1, 'take').startswith('MAPPED')
            assert protocol.ask(1, 'finish') == f'DONE {value * elements}'
        else:
            answer = protocol.receive(1)
            assert answer.startswith('RECEIVED ')
            data = json.loads(answer.split(' ', 1)[1])
            assert data['sum'] == value * elements
            assert data['dtype'] == 'torch.' + dtype
    if consumer == 'python':
        assert protocol.ask(1, 'hold') == 'HOLDING'
        protocol.publish(23)
        assert protocol.receive(1).startswith('RECEIVED ')
        assert protocol.ask(1, 'close') == 'BUSY'
        assert protocol.ask(1, 'drop') == 'DROPPED'
        deadline = time.monotonic() + 3
        while protocol.ask(1, 'close') != 'CLOSED':
            assert time.monotonic() < deadline
            time.sleep(0.01)
        # Recreating endpoints must not seal the shared runtime mapper or retain
        # wrapper event owners after the final Tensor and stream work have gone.
        for value in (31, 47):
            assert protocol.ask(1, 'recreate') == 'RECREATED'
            protocol.publish(value)
            answer = protocol.receive(1)
            assert json.loads(answer.split(' ', 1)[1])['sum'] == value * elements
            deadline = time.monotonic() + 3
            while protocol.ask(1, 'close') != 'CLOSED':
                assert time.monotonic() < deadline
                time.sleep(0.01)
            stats = json.loads(protocol.ask(1, 'stats').split(' ', 1)[1])
            assert stats['active_owners'] == stats['pending_owners'] == stats['wrapper_events'] == 0
    # The C++ probe uses its explicit drain command; G4 Python handles TERM.
    for index, language in enumerate((producer, consumer)):
        if language == 'cpp':
            epoch.send(index, 'quit')
    successful(epoch.wait(timeout=10) if 'cpp' in (producer, consumer) else epoch.stop())


@pytest.mark.parametrize('crashed_peer', [0, 1], ids=['producer-crash', 'consumer-crash'])
def test_tensor_crash_recovery_preserves_independent_epoch(epochs, crashed_peer):
    def pair():
        topic = '/g4_crash_' + uuid.uuid4().hex
        epoch = epochs([peer('python', 'publisher', topic), peer('python', 'subscriber', topic)])
        protocol = Protocol(epoch)
        assert protocol.receive(0).startswith('READY')
        assert protocol.receive(1).startswith('READY')
        return epoch, protocol

    healthy, healthy_protocol = pair()
    crashed, protocol = pair()
    assert protocol.ask(1, 'hold') == 'HOLDING'
    protocol.publish(29)
    assert protocol.receive(1).startswith('RECEIVED ')
    send_signal(crashed.peers[crashed_peer], signal.SIGKILL)
    record = crashed.wait(timeout=12)
    assert record['verified_teardown'] and record['echild'] and record['cleanup_complete'], record
    assert record['peer_failed'] and record['returncodes'][crashed_peer] == -signal.SIGKILL, record
    # The surviving peer reports the retained/dead-reader owner as a bounded
    # drain failure; the supervisor must not have to kill it as well.
    assert record['returncodes'][1 - crashed_peer] == 70, record
    assert all(item['returncode'] == 0 for item in record['descendants']), record
    healthy_protocol.publish(41)
    assert json.loads(healthy_protocol.receive(1).split(' ', 1)[1])['sum'] == 64 * 41
    replacement, replacement_protocol = pair()
    replacement_protocol.publish(53)
    assert json.loads(replacement_protocol.receive(1).split(' ', 1)[1])['sum'] == 64 * 53
    successful(replacement.stop())
    successful(healthy.stop())
