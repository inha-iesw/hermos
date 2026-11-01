"""Actual CUDA ownership behavior independent of descriptor middleware."""

import json
from pathlib import Path
import sys

import pytest

from support import successful


@pytest.mark.parametrize('case', ['aliases', 'cancel', 'producer_ready', 'capsule',
                                 'layout', 'generations', 'joined_streams', 'stream_mismatch'])
def test_native_tensor_ownership(epochs, tmp_path, case):
    output = tmp_path / 'result.json'
    control = tmp_path / 'control'
    control.mkdir()
    script = Path(__file__).with_name('native_peer.py')
    environment = {'AGNOCAST_BRIDGE_MODE': 'off', 'AGNOCAST_NO_DISCOVERY_AGENT': '1'}
    commands = [{'argv': [sys.executable, '-u', str(script), case, str(output), str(control)],
                 'env': environment.copy()}]
    if case != 'cancel':
        source = Path(__file__).with_name('native_source.py')
        commands.append({'argv': [sys.executable, '-u', str(source), str(control)],
                         'env': environment.copy()})
    epoch = epochs(commands)
    record = epoch.wait(timeout=30)
    successful(record)
    proof = json.loads(output.read_text())
    assert proof['case'] == case and proof['passed']
    assert proof['stats']['active_owners'] == proof['stats']['pending_owners'] == 0
    if case != 'cancel':
        source = json.loads((control / 'source-proof.json').read_text())
        assert source == proof['source']
        assert source['finished'] and source['pid'] != proof['pid']
        assert source['stats']['active_owners'] == source['stats']['pending_owners'] == 0
