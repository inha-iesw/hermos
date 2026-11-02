"""Separate rate-only diagnosis of large DDS sample loss; never replaces baseline."""

import json

from .observation import network_snapshot
from .protocol import RMWS, validate
from .provenance import archive_sources, capture
from .execute import run_pair
from experiments.g5.harness import run_matrix


def run_case(case, protocol, path):
    before = network_snapshot()
    result = run_pair(case, dict(protocol, period_ns=case['period_ns']), path)
    after = network_snapshot()
    observation = dict(before=before, after=after,
                       udp_delta={key: value-before['udp'][key]
                                  for key, value in after['udp'].items()})
    (path / 'network.json').write_text(json.dumps(observation, indent=2) + '\n')
    result['network'] = observation
    return result


def report(results, output, protocol):
    lines = ['# DDS rate-only diagnostic', '',
             '| RMW | Target Hz | Measured receipts | Lost | UDP InErrors delta | '
             'UDP RcvbufErrors delta |', '|---|---:|---:|---:|---:|---:|']
    for result in results:
        case, counters = result['case'], result['network']['udp_delta']
        lines.append(f"| {case['rmw']} | {case['rate_hz']} | {result['received']} | "
                     f"{result['lost']} | {counters['InErrors']} | {counters['RcvbufErrors']} |")
    lines += ['', 'Network counters cover the network namespace including other traffic and '
              'the warm-up/teardown periods; they are not packet-level causal attribution.',
              'Peer resources_before/after_warmup/after include CPU seconds, context switches '
              'and UDP socket drops filtered to owned inodes. Socket presence alone does not '
              'identify the DDS payload transport.',
              'No sysctl, DDS XML profile or reliability setting was changed. '
              'Original CPU/relay results remain separate.', '']
    (output / 'report.md').write_text('\n'.join(lines))


def run(output, *, smoke):
    """Keep size, QoS and profiles fixed while varying only target publication rate."""
    protocol = dict(sizes=[1048576], layouts=['dynamic'], rmws=list(RMWS),
                    transports=['dds'], samples=16 if smoke else 1000, warmup=100,
                    depth=4, full_check=smoke, order_seed=20261101,
                    rates_hz=[100, 20], network_diagnostics=True,
                    purpose='DDS rate-only loss diagnosis',
                    requirement_verdict='not-assessed: diagnostic, no acceptance claim')
    matrix = [dict(name=f'dynamic-1048576-{rmw}-dds-{hz}hz', size=1048576,
                   layout='dynamic', type='Dynamic', transport='dds', rmw=rmw,
                   rate_hz=hz, period_ns=1000000000 // hz)
              for rmw in protocol['rmws'] for hz in protocol['rates_hz']]
    for case in matrix:
        validate(dict(protocol, period_ns=case['period_ns']))
    run_matrix(output, protocol, matrix, capture=capture,
               archive_sources=archive_sources, run_case=run_case, report=report)
