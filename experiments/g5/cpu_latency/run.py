"""Run matched CPU latency pairs inside verified, fresh private namespaces."""

import argparse
import json
import os
from pathlib import Path

from experiments.g5.harness import paired_repeats, run_matrix
from .execute import run_pair
from .protocol import TYPES, RMWS, cases
from .provenance import archive_sources, capture


def report(results, path, protocol):
    repeated = any('repeat' in result['case'] for result in results)
    repeat_heading = 'Repeat | ' if repeated else ''
    repeat_rule = '---:|' if repeated else ''
    metrics = [('publish_to_callback_ns', 'mean'), ('publish_to_callback_ns', 'p99'),
               ('fill_to_consumer_ns', 'mean'), ('fill_to_consumer_ns', 'p99')]
    headings = ('Publish→callback mean | Publish→callback p99 | '
                'Fill→consumer mean | Fill→consumer p99')
    lines = ['# G5 synthetic CPU latency', '',
             'Raw timestamps use the same host CLOCK_MONOTONIC. This is not a VLN/rosbag result.',
             'The 10% research requirement remains unassessed pending the real workload.', '',
             'All latency columns below use microseconds.', '',
             f'| {repeat_heading}Layout | Bytes | RMW | Transport | Received / expected | {headings} | '
             'Publish-call mean |', f'|{repeat_rule}---|---:|---|---|---:|---:|---:|---:|---:|---:|']
    for result in results:
        case = result['case']
        values = [f"{result[metric][stat]/1000:.2f}" if result[metric] else 'N/A'
                  for metric, stat in metrics + [('publish_call_ns', 'mean')]]
        repeat_label = f"{case.get('repeat', '-')} | " if repeated else ''
        lines.append(
            f"| {repeat_label}{case['layout']} | {case['size']} | {case['rmw']} | {case['transport']} | "
            f"{result['received']}/{result['expected']} | " + ' | '.join(values) + ' |')
    lines += ['', '## Matched-RMW latency change', '',
              'Positive percentages mean additional latency: (proposed / DDS - 1) × 100. '
              'Loss invalidates a comparison.', '',
              f'| {repeat_heading}Layout | Bytes | RMW | {headings} |',
              f'|{repeat_rule}---|---:|---|---:|---:|---:|---:|']
    index = {
        (r['case'].get('repeat'), r['case']['layout'], r['case']['size'],
         r['case']['rmw'], r['case']['transport']): r
        for r in results}
    for key, proposed in index.items():
        # Historical evidence keeps its original transport identity.
        if key[-1] not in ('hermos', 'herm0s', 'agnocast') or (baseline := index.get((*key[:-1], 'dds'))) is None:
            continue
        changes = []
        for metric, stat in metrics:
            current, reference = proposed[metric], baseline[metric]
            if not proposed['complete'] or not baseline['complete']:
                changes.append('INVALID (message loss)')
            elif not current or not reference or not reference[stat]:
                changes.append('INVALID (unavailable or zero baseline)')
            else:
                changes.append(f"{100 * (current[stat] / reference[stat] - 1):+.2f}%")
        repeat_label = f"{key[0] if key[0] is not None else '-'} | " if repeated else ''
        lines.append(f'| {repeat_label}{key[1]} | {key[2]} | {key[3]} | ' + ' | '.join(changes) + ' |')
    lines += ['', '## Limits', '',
              '- CPU direct-fill synthetic messages; '
              'no actual VLN, GPU, bridge or native C++ IPC baseline.',
              '- Process RSS/high-water/FD snapshots are raw evidence, not proof of leak freedom.',
              '- Delivery rate uses (received - 1) / (last callback - first callback); '
              'paced observations are not saturation throughput.',
              '- First/last payload bytes are checked during timing; '
              'full content is checked in separate smoke runs.',
              f"- Protocol: {json.dumps(protocol, sort_keys=True)}", '']
    if protocol.get('resource_diagnostics', False):
        lines += ['Resource diagnostics enabled: heavy boundary observations can disturb pacing. '
                  'These are diagnostic results, not headline latency measurements.', '']
    (path / 'report.md').write_text('\n'.join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--resource-diagnostics', action='store_true',
                        help='separate diagnostic run with heavy resource reads at sample boundaries')
    parser.add_argument('--repeats', type=int, default=1,
                        help='positive matched repeats; >1 alternates adjacent DDS/Hermos pairs')
    parser.add_argument('--rate-diagnostic', action='store_true',
                        help='separate 1 MiB DDS 100/20 Hz diagnosis with socket counters')
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('--repeats must be a positive integer')
    if not args.output.is_absolute():
        parser.error('--output must be a fresh absolute path')
    if os.environ.get('LD_PRELOAD'):
        parser.error('supervisor must not preload Hermos')
    if args.rate_diagnostic:
        if args.repeats != 1:
            parser.error('--repeats is only supported by the matched latency matrix')
        if args.resource_diagnostics:
            parser.error('--resource-diagnostics requires the matched latency matrix')
        from .rate_diagnostic import run
        return run(args.output, smoke=args.smoke)
    protocol = dict(sizes=list(TYPES), layouts=['static', 'dynamic'], rmws=list(RMWS),
                    transports=['dds', 'hermos'], samples=16 if args.smoke else 1000,
                    warmup=100, period_ns=10000000, depth=4,
                    full_check=args.smoke, order_seed=20261101, repeats=args.repeats,
                    resource_diagnostics=args.resource_diagnostics,
                    case_order='seeded-shuffle',
                    purpose='functional-smoke' if args.smoke else 'synthetic-latency',
                    requirement_verdict='not-assessed: real VLN/rosbag required')
    matrix = paired_repeats(cases(protocol), args.repeats, transport_key='transport',
                            order_seed=protocol['order_seed'])
    if args.repeats > 1:
        protocol.update(case_order='repeat-major-seeded-pair-groups',
                        pair_transport_order='dds-first-odd-hermos-first-even')
    run_matrix(args.output, protocol, matrix, capture=capture,
               archive_sources=archive_sources, run_case=run_pair, report=report,
               shuffle=args.repeats == 1)


if __name__ == '__main__':
    main()
