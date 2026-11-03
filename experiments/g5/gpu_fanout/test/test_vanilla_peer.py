"""Validate the baseline's whole-payload checksum and bounded sequence contract."""

import pytest

from experiments.g5.gpu_fanout.vanilla_peer import validate_checksum


def test_full_payload_and_header_are_both_accounted():
    validate_checksum(257, (1024-8)*(257 % 251)+2, size=1024, total=400, message_sequence=257)


@pytest.mark.parametrize('sequence,actual,message_sequence', [
    (-1, 0, -1), (400, 0, 400), (2, 0, 2), (2, (1024-8)*2+2, 3),
])
def test_corruption_or_wrong_sequence_is_a_protocol_error(sequence, actual, message_sequence):
    with pytest.raises(RuntimeError, match='sequence|checksum'):
        validate_checksum(sequence, actual, size=1024, total=400, message_sequence=message_sequence)
