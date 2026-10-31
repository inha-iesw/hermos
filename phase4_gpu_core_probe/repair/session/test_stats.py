"""Check the peer protocol consumer without creating a CUDA process."""

import importlib.util
from pathlib import Path

import pytest


SOURCE = Path(__file__).resolve().parents[1] / 'test/test_transport.py'
spec = importlib.util.spec_from_file_location('transport_protocol', SOURCE)
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


def test_receipt_fields_match_the_peer_protocol():
    event = 'RECEIPT_STATS 1 1 1 1 1 2 2 1 1 0 3 6 6 3'.split()
    assert transport.parse_stats(event, 'RECEIPT_STATS', transport.RECEIPT_FIELDS) == {
        'fds': 1, 'cloexec': 1, 'closed': 1, 'context_restored': 1,
        'retains': 1, 'pushes': 2, 'pops': 2, 'releases': 1, 'imports': 1, 'refs': 0,
        'lifetime_retains': 3, 'lifetime_pushes': 6, 'lifetime_pops': 6,
        'lifetime_releases': 3}


@pytest.mark.parametrize('event', [
    'VMM_STATS 0 0 0 0 0 0 0 0 0',
    'VMM_STATS 0 0 0 0 0 0 0 0 0 1 99',
    'RECEIPT_STATS 0 0 0 0 0 0 0 0 0 1',
])
def test_malformed_protocol_is_not_silently_truncated(event):
    with pytest.raises(AssertionError):
        transport.parse_stats(event.split(), 'VMM_STATS', transport.VMM_FIELDS)


def test_no_vmm_expected_fields_distinguish_import_failure_from_later_stages():
    for fault, _, expected, diagnostic in transport.RECEIPT_CASES[:3]:
        assert expected == {'fd_closes': 1}, fault
        assert diagnostic.endswith(':'), fault


def test_context_activation_diagnostic_excludes_cleanup_activation_failure():
    diagnostic = transport.RECEIPT_CASES[1][3]
    assert diagnostic == 'Failed to activate CUDA context:'
    assert diagnostic not in 'Failed to activate CUDA context for imported resource cleanup:'


def test_bind_fields_distinguish_attempts_and_successful_cleanup():
    event = 'BIND_STATS 3 2 2 3 2'.split()
    assert transport.parse_stats(event, 'BIND_STATS', transport.BIND_FIELDS) == {
        'completion_creates': 3, 'completion_created': 2, 'ready_waits': 2,
        'ready_destroys': 3, 'completion_destroys': 2}


@pytest.mark.parametrize('event', [
    'BIND_STATS 1 1 1 1',
    'BIND_STATS 1 1 1 1 1 1',
    'VMM_STATS 1 1 1 1 1',
])
def test_malformed_bind_protocol_is_rejected(event):
    with pytest.raises(AssertionError):
        transport.parse_stats(event.split(), 'BIND_STATS', transport.BIND_FIELDS)
