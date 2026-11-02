"""Independent success and matrix-continuation oracles for G5 control code."""

import json
import sys
from types import SimpleNamespace

import pytest

from experiments.g5.harness import execute_epoch, peer_environment, run_matrix, verify_teardown


def teardown(peers=2, **changes):
    result = dict(verified_teardown=True, teardown_complete=True, echild=True,
                  peer_failed=False, returncodes=[0] * peers, descendants=[{'returncode': 0}])
    return dict(result, **changes)


@pytest.mark.parametrize('peers', [2, 3])
def test_teardown_accepts_exact_success_for_the_declared_peer_count(peers):
    verify_teardown(teardown(peers), peers)


@pytest.mark.parametrize('changes', [
    {'returncodes': [False, 0]}, {'returncodes': [0.0, 0]}, {'returncodes': [0]},
    {'returncodes': [0, 0, 0]}, {'returncodes': [0, 70]}, {'returncodes': []},
    {'descendants': [{'returncode': False}]}, {'descendants': [{'returncode': 0.0}]},
    {'descendants': [{}]}, {'descendants': None}, {'descendants': [None]},
    {'verified_teardown': 1}, {'echild': False}, {'teardown_complete': False},
    {'peer_failed': 0},
])
def test_teardown_rejects_failure_or_malformed_success_evidence(changes):
    with pytest.raises(RuntimeError, match='teardown'):
        verify_teardown(teardown(**changes), 2)


def test_zero_receipts_preserve_results_and_run_remaining_cases(tmp_path):
    output = tmp_path / 'matrix'
    protocol = {'order_seed': 7}
    matrix = [{'name': 'all-lost'}, {'name': 'delivered'}]
    called, reports = [], []
    identity = {'source_sha256': {'source.py': 'frozen'}}

    def archive(value, path):
        assert value == identity
        path.write_bytes(b'archive')

    def execute(case, settings, path):
        assert settings == protocol
        assert (output / 'sources.tar.gz').read_bytes() == b'archive'
        assert json.loads((output / 'protocol.json').read_text())['cases'] == matrix
        assert json.loads((output / 'provenance.json').read_text()) == identity
        called.append(case['name'])
        complete = case['name'] == 'delivered'
        return dict(case=case, received=3 if complete else 0, expected=3,
                    lost=0 if complete else 3, complete=complete)

    def report(results, path, settings):
        reports.append(len(results))

    with pytest.raises(SystemExit) as error:
        run_matrix(output, protocol, matrix, capture=lambda: identity, archive_sources=archive,
                   run_case=execute, report=report, shuffle=False)
    assert error.value.code == 2
    assert called == ['all-lost', 'delivered']
    assert reports == [1, 2]
    summary = json.loads((output / 'summary.json').read_text())
    assert summary['all_messages_received'] is False
    assert [r['received'] for r in summary['results']] == [0, 3]


def test_protocol_errors_abort_instead_of_becoming_loss(tmp_path):
    def execute(*_):
        raise ValueError('invalid timestamp order')

    with pytest.raises(ValueError, match='timestamp'):
        run_matrix(tmp_path / 'matrix', {}, [{'name': 'bad'}, {'name': 'unreached'}],
                   capture=lambda: {}, archive_sources=lambda *args: None,
                   run_case=execute, report=lambda *args: None, shuffle=False)


def test_changed_identity_rejects_an_otherwise_complete_matrix(tmp_path):
    identities = iter([{'revision': 1}, {'revision': 2}])
    with pytest.raises(RuntimeError, match='changed during measurement'):
        run_matrix(tmp_path / 'matrix', {}, [{'name': 'complete'}],
                   capture=lambda: next(identities), archive_sources=lambda *args: None,
                   run_case=lambda *args: {'complete': True}, report=lambda *args: None,
                   shuffle=False)
    assert not (tmp_path / 'matrix' / 'summary.json').exists()


def test_transport_environment_only_preloads_the_hermos_peer(tmp_path):
    hook = tmp_path / 'libhermos_heaphook.so'
    dds = peer_environment('test-rmw', hermos=False, heaphook=hook)
    hermos = peer_environment('test-rmw', hermos=True, heaphook=hook)
    assert 'LD_PRELOAD' not in dds
    assert hermos.pop('LD_PRELOAD') == str(hook)
    assert hermos == dds
    assert dds['RMW_IMPLEMENTATION'] == 'test-rmw'
    assert dds['HERMOS_BRIDGE_MODE'] == 'off'


@pytest.mark.parametrize('peers', [2, 3])
def test_epoch_saves_every_report_before_teardown(tmp_path, monkeypatch, peers):
    saved = []

    class Epoch:
        def __init__(self, commands, path, **options):
            self.epoch_dir = path
            self.epoch_dir.mkdir()
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
                self.emit(index, 'SENT', count=2)
            elif command == 'count':
                self.emit(index, 'COUNT', count=2)
            elif command == 'finish':
                saved.append(index)
                self.emit(index, 'SAVED')

        def poll(self):
            return None

        def stop(self):
            assert saved == list(range(self.peers))
            return teardown(self.peers)

    monkeypatch.setitem(sys.modules, 'isolated', SimpleNamespace(IsolatedEpoch=Epoch))
    monkeypatch.setattr('experiments.g5.harness.time.sleep', lambda _: None)
    execute_epoch([{} for _ in range(peers)], tmp_path,
                  expected=2, period_ns=10000000, startup_timeout=30)
    assert json.loads((tmp_path / 'teardown.json').read_text()) == teardown(peers)
