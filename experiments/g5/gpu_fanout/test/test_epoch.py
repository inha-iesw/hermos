"""Drain every receiver under one deadline and preserve failed-epoch teardown."""

import json
import sys
from types import SimpleNamespace

import pytest

from experiments.g5.gpu_fanout import epoch


def teardown_evidence(peers):
    return dict(verified_teardown=True, teardown_complete=True, echild=True,
                peer_failed=False, returncodes=[0] * peers, descendants=[])


def test_drain_queries_every_receiver_and_retains_loss_at_one_shared_deadline(monkeypatch):
    now = [0.0]
    queries = []

    class Events:
        def __init__(self):
            self.epoch = self

        def send(self, index, command):
            assert command == 'count'
            queries.append(index)

        def receive(self, index, event, timeout):
            assert event == 'COUNT' and 0 < timeout <= 1
            return {'count': 1 if index == 1 else 3}

    monkeypatch.setattr(epoch.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(epoch.time, 'sleep', lambda delay: now.__setitem__(0, now[0] + delay))
    counts = epoch.drain_subscribers(Events(), (1, 2, 3), 3, timeout=.1)
    assert counts == {1: 1, 2: 3, 3: 3}
    assert queries[:3] == [1, 2, 3]
    assert queries.count(2) == queries.count(3) == 1
    assert now[0] == pytest.approx(.1)


def test_last_round_near_payload_deadline_allows_normal_control_response(monkeypatch):
    now = [0.0]
    queries = []

    class Events:
        def __init__(self):
            self.epoch = self

        def send(self, index, command):
            queries.append((index, now[0]))

        def receive(self, index, event, timeout):
            delay = .005 if index == 1 else 0
            if timeout < delay:
                raise TimeoutError('normal 5 ms COUNT response was cut short')
            now[0] += delay
            return {'count': 2}

    monkeypatch.setattr(epoch.time, 'monotonic', lambda: now[0])
    # Simulate scheduling overhead: the final round begins at 29 of 30 ms.
    monkeypatch.setattr(epoch.time, 'sleep', lambda _: now.__setitem__(0, .029))
    counts = epoch.drain_subscribers(Events(), (1, 2), 12, timeout=.03)
    assert counts == {1: 2, 2: 2}
    assert queries == [(1, 0), (2, 0), (1, .029), (2, .029)]
    assert now[0] == pytest.approx(.034)


def test_no_new_control_round_begins_at_payload_deadline(monkeypatch):
    now = [0.0]
    queries = []
    events = SimpleNamespace(
        epoch=SimpleNamespace(send=lambda index, command: queries.append(now[0])),
        receive=lambda *_, **__: {'count': 2})
    monkeypatch.setattr(epoch.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(epoch.time, 'sleep', lambda _: now.__setitem__(0, .1))
    assert epoch.drain_subscribers(events, (1,), 12, timeout=.1) == {1: 2}
    assert queries == [0]


def test_count_response_budget_is_shared_across_subscribers(monkeypatch):
    now = [0.0]
    timeouts = []

    def receive(index, event, timeout):
        timeouts.append(timeout)
        if index == 1:
            now[0] += .75
            return {'count': 2}
        raise TimeoutError('second reader did not answer within the round budget')

    events = SimpleNamespace(epoch=SimpleNamespace(send=lambda *_: None), receive=receive)
    monkeypatch.setattr(epoch.time, 'monotonic', lambda: now[0])
    with pytest.raises(TimeoutError, match='second reader'):
        epoch.drain_subscribers(events, (1, 2), 12)
    assert timeouts == pytest.approx([1, .25])


@pytest.mark.parametrize('bad_count', [True, -1, 4, '3'])
def test_malformed_receipt_count_is_not_silently_treated_as_loss(bad_count):
    events = SimpleNamespace(epoch=SimpleNamespace(send=lambda *_: None),
                             receive=lambda *_, **__: {'count': bad_count})
    with pytest.raises(RuntimeError, match='count'):
        epoch.drain_subscribers(events, (1,), 3)


@pytest.mark.parametrize('publication_count', [2, 1])
def test_epoch_saves_all_reports_or_preserves_failure_teardown(tmp_path, monkeypatch, publication_count):
    saved, counted = [], []

    class Epoch:
        def __init__(self, commands, path, **options):
            self.epoch_dir = path
            path.mkdir()
            self.peers = len(commands)

        def emit(self, index, event, **values):
            with (self.epoch_dir / f'peer-{index}.stdout').open('a') as output:
                output.write(json.dumps(dict(event=event, **values)) + '\n')

        def start(self):
            for index in range(self.peers):
                self.emit(index, 'READY')

        def send(self, index, command):
            if command == 'connections':
                self.emit(index, 'CONNECTED', count=1)
            elif command == 'start':
                self.emit(index, 'SENT', count=publication_count)
            elif command == 'count':
                counted.append(index)
                self.emit(index, 'COUNT', count=2)
            elif command == 'finish':
                saved.append(index)
                self.emit(index, 'SAVED')

        def poll(self):
            return None

        def stop(self):
            if publication_count == 2:
                assert saved == [0, 1, 2]
            return teardown_evidence(3)

    monkeypatch.setitem(sys.modules, 'hermos_runtime.supervision.isolated',
                        SimpleNamespace(IsolatedEpoch=Epoch))
    monkeypatch.setattr(epoch.time, 'sleep', lambda _: None)
    if publication_count == 2:
        epoch.execute_fanout([{}, {}, {}], tmp_path, expected=2, period_ns=1)
        assert counted == [1, 2]
    else:
        with pytest.raises(RuntimeError, match='publication'):
            epoch.execute_fanout([{}, {}, {}], tmp_path, expected=2, period_ns=1)
        assert saved == []
    assert json.loads((tmp_path / 'teardown.json').read_text()) == teardown_evidence(3)
