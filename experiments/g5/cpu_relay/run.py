"""Measure explicit CPU relay paths against a matched DDS-to-DDS copy relay."""

import argparse
import itertools
import json
import os
from pathlib import Path
import sys

from experiments.g5.cpu_latency.protocol import RMWS, validate
from experiments.g5.cpu_latency.provenance import PREFIX, archive_sources, capture, verify_peer
from experiments.g5.harness import execute_epoch, peer_environment, run_matrix
from .analysis import analyze_chain


def cases(protocol=None):
    """Freeze all three paths, two RMWs and three dynamic payload sizes."""
    sizes, rmws = (1024, 65536, 1048576), RMWS
    if protocol is not None:
        validate(protocol)
        if (protocol['layouts'] != ['dynamic'] or
                set(protocol['transports']) != {'dds', 'hermos'}):
            raise ValueError('relay requires dynamic layout and both DDS/Hermos transports')
        sizes, rmws = protocol['sizes'], protocol['rmws']
    paths = (('dds', 'dds'), ('hermos', 'dds'), ('dds', 'hermos'))
    return [dict(name=f'dynamic-{size}-{rmw}-{incoming}-to-{outgoing}',
                 size=size, layout='dynamic', type='Dynamic', rmw=rmw,
                 input=incoming, output=outgoing, path=f'{incoming}-to-{outgoing}')
            for size, rmw, (incoming, outgoing) in
            itertools.product(sizes, rmws, paths)]


def run_chain(case, protocol, path):
    path.mkdir()
    input_topic, output_topic = (f'/g5/p{os.getpid()}/relay/{side}' for side in ('in', 'out'))
    configs = [
        dict(case=dict(case, transport=case['input']), protocol=protocol, topic=input_topic),
        dict(case=case, protocol=protocol, input_topic=input_topic, output_topic=output_topic),
        dict(case=dict(case, transport=case['output']), protocol=protocol, topic=output_topic),
    ]
    proposed = [case['input'] == 'hermos',
                'hermos' in (case['input'], case['output']), case['output'] == 'hermos']
    commands = []
    for index, role in enumerate(('source', 'relay', 'sink')):
        config_path = path / f'{role}-case.json'
        config_path.write_text(json.dumps(configs[index], indent=2) + '\n')
        module = ('experiments.g5.cpu_relay.peer' if role == 'relay' else
                  'experiments.g5.cpu_latency.peer')
        argv = [sys.executable, '-u', '-m', module, '--case', str(config_path),
                '--output', str(path / f'{role}.json')]
        if role != 'relay':
            argv += ['--role', 'publisher' if role == 'source' else 'subscriber']
        environment = peer_environment(
            case['rmw'], hermos=proposed[index],
            heaphook=PREFIX / 'hermoslib/lib/libhermos_heaphook.so')
        commands.append(dict(argv=argv, env=environment))
    execute_epoch(commands, path, expected=protocol['warmup'] + protocol['samples'],
                  period_ns=protocol['period_ns'], startup_timeout=30)
    records = [json.loads((path / f'{role}.json').read_text())
               for role in ('source', 'relay', 'sink')]
    for record, uses_hermos in zip(records, proposed):
        verify_peer(record, uses_hermos)
    if len({record['time_namespace'] for record in records}) != 1:
        raise RuntimeError('relay timestamps require one host time namespace')
    result = analyze_chain(*records, warmup=protocol['warmup'], samples=protocol['samples'])
    result['case'] = case
    (path / 'analysis.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def report(results, path, protocol=None):
    lines = ['# G5 explicit CPU relay', '',
             'Each path has three processes and an explicit full payload copy at the relay.',
             'This is a private-epoch experiment, not the native Hermos auto-bridge.', '',
             '| Bytes | RMW | Path | Sink received/expected | Missing relay rows | '
             'Mean µs | p99 µs | Valid |',
             '|---:|---|---|---:|---:|---:|---:|---|']
    for result in results:
        case, delivery = result['case'], result['source_to_sink']
        stats = delivery.get('publish_to_callback_ns')
        mean = f"{stats['mean']/1000:.2f}" if stats else 'N/A'
        p99 = f"{stats['p99']/1000:.2f}" if stats else 'N/A'
        lines.append(
            f"| {case['size']} | {case['rmw']} | {case['path']} | "
            f"{delivery['received']}/{delivery['expected']} | {result['missing_relay_records']} | "
            f"{mean} | {p99} | {result['complete']} |")
    lines += ['', '## End-to-end difference versus matched DDS-to-DDS relay', '',
              'Positive percentages mean additional latency. Loss invalidates a comparison.', '']
    controls = {(r['case']['size'], r['case']['rmw']): r for r in results
                if r['case']['path'] == 'dds-to-dds'}
    for result in results:
        case = result['case']
        baseline = controls.get((case['size'], case['rmw']))
        if case['path'] == 'dds-to-dds' or baseline is None:
            continue
        if not result['complete'] or not baseline['complete']:
            value = 'INVALID (incomplete delivery or relay evidence)'
        else:
            current = result['source_to_sink']['publish_to_callback_ns']['mean']
            reference = baseline['source_to_sink']['publish_to_callback_ns']['mean']
            value = (f'{current-reference:+.0f} ns, {(current/reference-1)*100:+.2f}%'
                     if reference else 'INVALID (zero baseline interval)')
        lines.append(f"- {case['size']} bytes, {case['rmw']}, {case['path']}: {value}")
    lines += ['', '## Scope', '',
              '- Warm-up is excluded from latency statistics; missing relay rows remain visible.',
              '- Copy, allocation and middleware publication costs are included end-to-end.',
              '- All peers remain in one registered private epoch; '
              'bridge/discovery stay disabled.',
              '- No GPU tensor bridging, rosbag/VLN evaluation, or native auto-bridge claim.',
              '- RSS/FD snapshots are observations, not proof of leak freedom.',
              '- Synthetic overhead measurements do not certify '
              'the final research requirement.', '']
    (path / 'report.md').write_text('\n'.join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--smoke', action='store_true')
    args = parser.parse_args()
    if not args.output.is_absolute():
        parser.error('--output must be a fresh absolute path')
    if os.environ.get('LD_PRELOAD'):
        parser.error('supervisor must not preload Hermos')
    protocol = dict(sizes=[1024, 65536, 1048576], layouts=['dynamic'], rmws=list(RMWS),
                    transports=['dds', 'hermos'], samples=16 if args.smoke else 1000,
                    warmup=100, period_ns=20000000, depth=4, full_check=args.smoke,
                    order_seed=20261101,
                    purpose='relay-functional-smoke' if args.smoke else 'synthetic-relay-latency',
                    requirement_verdict='not-assessed: explicit CPU relay experiment')
    run_matrix(args.output, protocol, cases(protocol), capture=capture,
               archive_sources=archive_sources, run_case=run_chain, report=report)


if __name__ == '__main__':
    main()
