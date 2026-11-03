"""Separate, small GPU call-cost diagnostics; never a headline latency comparison."""

import argparse
import json
import os
from pathlib import Path

from experiments.g5.gpu_latency import run as base
from experiments.g5.harness import run_matrix
from .analysis import summarize


def run_case(config, protocol, path):
    """Reuse validated device orchestration, then require one cost record per raw row."""
    result = base.run_pair(config, path,
                           peer_factory='experiments.g5.gpu_latency.costs.peer:factory')
    costs = {}
    for role in ('publisher', 'subscriber'):
        value = json.loads((path / f'{role}-costs.json').read_text())
        raw = json.loads((path / f'{role}.json').read_text())
        if (value.get('version') != 1 or value.get('config') != config or
                value.get('role') != role or value.get('timing_diagnostics') is not True or
                value.get('headline_eligible') is not False):
            raise ValueError('invalid cost diagnostic identity')
        if [item['sequence'] for item in value['samples']] != [row[0] for row in raw['samples']]:
            raise ValueError('cost sequence rows differ from actual peer receipts')
        costs[role] = summarize(value['samples'], warmup=config['warmup'], samples=config['samples'])
    result.update(costs=costs, timing_diagnostics=True, headline_eligible=False)
    (path / 'costs-analysis.json').write_text(json.dumps(costs, indent=2) + '\n')
    return result


def report(results, path, protocol):
    lines = ['# GPU call-cost diagnostic', '',
             'Timing diagnostics only; instrumentation adds host work and these results are '
             'not eligible for headline performance comparisons.',
             'Descriptor fill total includes resize. Fill excluding resize still includes '
             'traversal, validation and instrumentation. Do not add overlapping stages.',
             'Tensor export total includes descriptor decode and native map. Remaining export '
             'is reported only when their intervals are contained and nonoverlapping; '
             'it includes DLPack/Torch export, validation and instrumentation overhead.',
             'DDS constructs an owning descriptor; shared descriptor borrow/resize stages '
             'are absent, not zero. GPU completion wait times the existing host wait only.', '',
             '| Case | Peer | Stage | Calls | Failed calls | Mean / p50 / p99 µs per sample |',
             '|---|---|---|---:|---:|---:|']
    for result in results:
        for role, costs in result['costs'].items():
            for stage, value in costs['stages'].items():
                stats = value['per_sample_ns']
                times = ' / '.join(f'{stats[key] / 1000:.3f}' for key in ('mean', 'p50', 'p99'))
                lines.append(f"| {result['case']['name']} | {role} | {stage} | "
                             f"{value['calls'] if value['calls'] is not None else 'derived'} | "
                             f"{value['failures'] if value['failures'] is not None else '—'} | {times} |")
    lines += ['', 'Receipt loss and teardown remain governed by the normal G5 runner. '
              'Raw timestamps and diagnostic clocks are saved separately.',
              f'Protocol: {json.dumps(protocol, sort_keys=True)}', '']
    (path / 'report.md').write_text('\n'.join(lines))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sizes', type=int, nargs='+', choices=(1024, 4194304), default=[1024, 4194304])
    parser.add_argument('--rmws', nargs='+', choices=base.RMWS, default=list(base.RMWS))
    parser.add_argument('--transports', nargs='+', choices=('dds', 'hermos'), default=['dds', 'hermos'])
    parser.add_argument('--samples', type=int, default=32)
    parser.add_argument('--warmup', type=int, default=16)
    args = parser.parse_args(argv)
    if not args.output.is_absolute() or os.environ.get('LD_PRELOAD'):
        parser.error('fresh absolute output and non-preloaded supervisor required')
    if args.samples < 1 or args.warmup < 0:
        parser.error('samples must be positive and warmup nonnegative')
    for values in (args.sizes, args.rmws, args.transports):
        if len(values) != len(set(values)):
            parser.error('diagnostic matrix choices must be unique')
    protocol = dict(sizes=args.sizes, rmws=args.rmws, transports=args.transports,
                    dtype='uint8', blocks=16, max_reads=16, depth=16, descriptor_bytes=4096,
                    retained_bytes_per_read=4096, warmup=args.warmup, samples=args.samples,
                    period_ns=20000000, diagnostic_dtoh_bytes_per_sample=16,
                    resource_diagnostics=False, timing_diagnostics=True, headline_eligible=False,
                    order_seed=20261101, case_order='seeded-shuffle',
                    requirement_verdict='not-assessed: instrumented call-cost diagnostic')
    matrix = [base.make_config(dict(case, timing_diagnostics=True, headline_eligible=False), protocol)
              for case in base.cases(True, args.sizes)
              if case['rmw'] in args.rmws and case['descriptor_transport'] in args.transports]
    run_matrix(args.output, protocol, matrix, capture=base.identity,
               archive_sources=base.archive_sources, run_case=run_case, report=report)


if __name__ == '__main__':
    main()
