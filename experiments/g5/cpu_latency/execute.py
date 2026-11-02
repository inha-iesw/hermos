"""Execute one CPU latency case; measurement stays in the peer processes."""

import json
import os
import sys

from experiments.g5.harness import execute_epoch, peer_environment
from .analysis import analyze
from .provenance import PREFIX, verify_peer


def run_pair(case, protocol, path):
    path.mkdir()
    case_path = path / 'case.json'
    config = dict(case=case, protocol=protocol, topic=f'/g5/p{os.getpid()}/payload')
    case_path.write_text(json.dumps(config, indent=2) + '\n')
    environment = peer_environment(
        case['rmw'], hermos=case['transport'] == 'hermos',
        heaphook=PREFIX / 'hermoslib/lib/libhermos_heaphook.so')
    commands = [
        dict(argv=[sys.executable, '-u', '-m', 'experiments.g5.cpu_latency.peer',
                   '--case', str(case_path), '--role', role,
                   '--output', str(path / f'{role}.json')], env=environment)
        for role in ('publisher', 'subscriber')]
    execute_epoch(commands, path, expected=protocol['warmup'] + protocol['samples'],
                  period_ns=protocol['period_ns'], startup_timeout=30)
    publisher = json.loads((path / 'publisher.json').read_text())
    subscriber = json.loads((path / 'subscriber.json').read_text())
    for record in (publisher, subscriber):
        verify_peer(record, case['transport'] == 'hermos')
    if publisher['time_namespace'] != subscriber['time_namespace']:
        raise RuntimeError('cross-process timestamps require the same time namespace')
    result = analyze(publisher, subscriber, warmup=protocol['warmup'], samples=protocol['samples'])
    result['case'] = case
    (path / 'analysis.json').write_text(json.dumps(result, indent=2) + '\n')
    return result
