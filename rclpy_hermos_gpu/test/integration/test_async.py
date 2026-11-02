"""Actual TERM resumes a suspended GPU callback before the peer exits cleanly."""

import json
import signal
import uuid

from support import Protocol, environment, launcher_argv, peer, send_signal, successful


def test_term_drains_async_callback_retaining_cuda_alias(epochs):
    topic = '/g4_async_' + uuid.uuid4().hex
    reader = {'argv': launcher_argv('async_peer:factory', topic),
              'env': environment()}
    epoch = epochs([peer('python', 'publisher', topic), reader])
    protocol = Protocol(epoch)
    assert protocol.receive(0).startswith('READY')
    assert protocol.receive(1).startswith('READY')
    protocol.publish(17)
    answer = protocol.receive(1)
    assert answer.startswith('HOLDING ')
    held = json.loads(answer.split(' ', 1)[1])
    assert held['pid'] == epoch.peers[1]
    assert held['sum'] == 17 * 64 and held['active_owners'] == 1
    send_signal(epoch.peers[1], signal.SIGTERM)
    answer = protocol.receive(1)
    assert answer.startswith('DONE ')
    assert json.loads(answer.split(' ', 1)[1]) == {
        'sum': 17 * 64, 'closing': True, 'callbacks': 1}
    # The canonical worker confirms both peer exit codes, descendant reap and
    # namespace teardown; a printed continuation result alone is insufficient.
    successful(epoch.stop())
