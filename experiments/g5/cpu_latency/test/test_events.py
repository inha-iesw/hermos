"""Control logs must not consume a peer's partially written JSON line."""

from types import SimpleNamespace

from experiments.g5.harness import Events


def test_event_reader_keeps_partial_tail_for_next_poll(tmp_path, monkeypatch):
    path = tmp_path / 'peer-0.stdout'
    path.write_text('native diagnostic\n{"event": "REA')
    epoch = SimpleNamespace(epoch_dir=tmp_path, poll=lambda: None)

    def finish(_):
        with path.open('a') as output:
            output.write('DY", "pid": 123}\n')
    monkeypatch.setattr('experiments.g5.harness.time.sleep', finish)
    events = Events(epoch, peers=2)
    assert events.receive(0, 'READY') == {'event': 'READY', 'pid': 123}
    assert events.offsets == [2, 0]


def test_three_peer_reader_keeps_each_stream_position_independent(tmp_path):
    for index in range(3):
        (tmp_path / f'peer-{index}.stdout').write_text(
            '{"event": "READY"}\n{"event": "SAVED"}\n')
    epoch = SimpleNamespace(epoch_dir=tmp_path, poll=lambda: None)
    events = Events(epoch, peers=3)
    assert events.receive(2, 'READY')['event'] == 'READY'
    assert events.receive(2, 'SAVED')['event'] == 'SAVED'
    assert events.receive(0, 'READY')['event'] == 'READY'
    assert events.offsets == [1, 0, 2]
