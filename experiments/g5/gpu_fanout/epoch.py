"""Bounded all-subscriber drain inside the existing owned private epoch."""

import json
import time

from experiments.g5.harness import Events, verify_teardown, wait_connected


COUNT_RESPONSE_SECONDS = 1


def drain_subscribers(events, indices, expected, *, timeout=5):
    """Start COUNT rounds for five seconds, allowing one second to finish each round.

    Failure to answer a control request is a protocol error, not payload loss.
    The response budget is shared across a round, rather than renewed per reader.
    A final round may finish after the payload deadline; total wait is at most
    the payload budget plus one response budget, apart from scheduling overhead.
    Finished receivers stop receiving COUNT requests while other readers drain.
    """
    pending = set(indices)
    counts = {}
    deadline = time.monotonic() + timeout
    while pending and time.monotonic() < deadline:
        response_deadline = time.monotonic() + COUNT_RESPONSE_SECONDS
        for index in sorted(pending):
            events.epoch.send(index, 'count')
        for index in sorted(pending):
            remaining = response_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('COUNT round response deadline expired')
            count = events.receive(index, 'COUNT', timeout=remaining)['count']
            if type(count) is not int or not 0 <= count <= expected:
                raise RuntimeError(f'invalid receiver count from peer {index}: {count}')
            counts[index] = count
            if count == expected:
                pending.remove(index)
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(min(.02, max(0, deadline - time.monotonic())))
    return counts


def execute_fanout(commands, path, *, expected, period_ns, startup_timeout=150):
    """Save every report before stopping peers, retaining teardown even on error."""
    from hermos_runtime.supervision.isolated import IsolatedEpoch

    peers = len(commands)
    if peers < 2:
        raise ValueError('fanout requires a publisher and at least one subscriber')
    epoch = IsolatedEpoch(commands, path / 'epoch',
                          startup_timeout=startup_timeout, stop_timeout=5)
    epoch.start()
    try:
        events = Events(epoch, peers)
        for index in range(peers):
            events.receive(index, 'READY', timeout=startup_timeout)
        wait_connected(events, range(1, peers))
        epoch.send(0, 'start')
        sent = events.receive(0, 'SENT', timeout=3 * expected * period_ns / 1e9 + 60)
        if type(sent.get('count')) is not int or sent['count'] != expected:
            raise RuntimeError(f'incomplete publication: {sent}')
        counts = drain_subscribers(events, range(1, peers), expected)
        (path / 'drain.json').write_text(json.dumps(
            dict(expected=expected, counts=counts, shared_deadline_seconds=5,
                 count_response_round_seconds=COUNT_RESPONSE_SECONDS,
                 maximum_control_drain_seconds=5 + COUNT_RESPONSE_SECONDS), indent=2) + '\n')
        for index in range(peers):
            epoch.send(index, 'finish')
            events.receive(index, 'SAVED', timeout=60)
    finally:
        evidence = epoch.stop()
        (path / 'teardown.json').write_text(json.dumps(evidence, indent=2) + '\n')
    verify_teardown(evidence, peers)
