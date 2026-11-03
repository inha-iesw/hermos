"""Repeated Python GPU fanout under matched pacing and explicit delivery contracts."""

import argparse
import json
import os
from pathlib import Path
import random
import sys

from experiments.g5.cpu_latency.provenance import PREFIX, archive_sources, verify_peer
from experiments.g5.gpu_latency.run import CORE_PREFIX, GPU_PREFIX, identity
from experiments.g5.harness import peer_environment, run_matrix
from .analysis import analyze_fanout, report
from .epoch import execute_fanout


def protocol(*, size=24883200, subscribers=(8,), rates=(5, 30), samples=300, warmup=64, repeats=3):
    """Bound the representative image/fanout matrix before any device activity."""
    for name, values, allowed in (('subscribers', subscribers, (1, 8)), ('rates', rates, (5, 30))):
        if (not values or len(set(values)) != len(values) or
                any(type(value) is not int or value not in allowed for value in values)):
            raise ValueError(f'invalid {name}')
    for name, value, minimum in (('samples', samples, 1), ('warmup', warmup, 0), ('repeats', repeats, 1)):
        if type(value) is not int or value < minimum:
            raise ValueError(f'invalid {name}')
    if type(size) is not int or size not in (65536, 24883200):
        raise ValueError('invalid size')
    return dict(size=size, subscribers=list(subscribers), rates_hz=list(rates),
                samples=samples, warmup=warmup, repeats=repeats,
                rmw='rmw_fastrtps_cpp', depth=4, blocks=8, max_reads=8,
                descriptor_bytes=4096, retained_bytes_per_read=4096,
                dtype='uint8', resource_diagnostics=False, order_seed=20261103,
                payload_comparison='pageable host staging versus CUDA IPC',
                pacing='ROS timer for both transports',
                requirement_verdict='not-assessed: synthetic, no real VLN')


def matrix(settings):
    """Seed-shuffle load groups, keeping transport pairs adjacent and alternating."""
    randomizer = random.Random(settings['order_seed'])
    groups = [(n, rate) for n in settings['subscribers'] for rate in settings['rates_hz']]
    result = []
    for repeat in range(1, settings['repeats'] + 1):
        order = list(groups)
        randomizer.shuffle(order)
        for subscribers, rate in order:
            transports = ('vanilla', 'hermos') if repeat % 2 else ('hermos', 'vanilla')
            for transport in transports:
                name = f"{settings['size']}-n{subscribers}-{rate}hz-{transport}-repeat-{repeat:02d}"
                result.append(dict(
                    name=name, scenario='gpu', variant='rclpy-' + transport,
                    size=settings['size'], subscribers=subscribers, rate_hz=rate,
                    period_ns=1_000_000_000 // rate, repeat=repeat, transport=transport,
                    descriptor_transport=transport,
                    topic=f"/g5/p{os.getpid()}/fanout_{name.replace('-', '_')}",
                    max_retained_bytes=settings['max_reads'] * settings['retained_bytes_per_read'],
                    **{key: settings[key] for key in ('rmw', 'depth', 'blocks', 'max_reads',
                       'descriptor_bytes', 'retained_bytes_per_read', 'dtype', 'warmup', 'samples',
                       'resource_diagnostics')}))
    return result


def peer_command(case, role, config_path, output_path):
    """HERM0S uses registered native owners; stock DDS registers an empty inventory."""
    if case['transport'] == 'hermos':
        return [sys.executable, '-u', '-m', 'rclpy_hermos_gpu.launch',
                '--blocks', str(case['blocks']) if role == 'publisher' else '0',
                '--block-bytes', str(case['size']), '--max-reads', str(case['max_reads']),
                '--max-message-bytes', str(case['size']),
                'experiments.g5.gpu_latency.peer:factory', role, str(config_path), str(output_path)]
    return [sys.executable, '-u', '-m', 'experiments.g5.gpu_fanout.vanilla_peer',
            '--role', role, '--config', str(config_path), '--output', str(output_path)]


def validate_record(record, case):
    """Reject mismatched records, artifacts or unsettled HERM0S owners before analysis."""
    if record.get('config') != case:
        raise ValueError('peer config differs from the frozen case')
    proposed = case['transport'] == 'hermos'
    verify_peer(record, proposed, prefixes=(PREFIX, GPU_PREFIX, CORE_PREFIX))
    if proposed:
        paths = [Path(path) for path in record['loaded_objects']]
        if not any(path.is_relative_to(GPU_PREFIX) and path.name.startswith('_native.') for path in paths):
            raise RuntimeError('GPU production native module missing')
        after = record['runtime_after']
        if after['active_owners'] or after['pending_owners']:
            raise RuntimeError(f'GPU owners did not settle: {after}')


def run_case(case, settings, path):
    """Execute one fresh fanout epoch and retain every recipient's raw observations."""
    path.mkdir()
    config_path = path / 'config.json'
    config_path.write_text(json.dumps(case, indent=2) + '\n')
    environment = peer_environment(case['rmw'], hermos=case['transport'] == 'hermos',
                                   heaphook=PREFIX / 'hermoslib/lib/libhermos_heaphook.so')
    roles = ['publisher', *[f'subscriber-{index}' for index in range(1, case['subscribers'] + 1)]]
    commands = [dict(argv=peer_command(case, 'publisher' if index == 0 else 'subscriber',
                                      config_path, path / f'{role}.json'), env=environment)
                for index, role in enumerate(roles)]
    execute_fanout(commands, path, expected=case['warmup'] + case['samples'],
                   period_ns=case['period_ns'])
    records = [json.loads((path / f'{role}.json').read_text()) for role in roles]
    for record in records:
        validate_record(record, case)
    result = analyze_fanout(case, records[0], records[1:])
    (path / 'analysis.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--size', type=int, choices=(65536, 24883200), default=24883200)
    parser.add_argument('--subscribers', type=int, nargs='+', choices=(1, 8), default=[8])
    parser.add_argument('--rates', type=int, nargs='+', choices=(5, 30), default=[5, 30])
    parser.add_argument('--samples', type=int, default=300)
    parser.add_argument('--warmup', type=int, default=64)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    if not args.output.is_absolute() or os.environ.get('LD_PRELOAD'):
        parser.error('fresh absolute output and non-preloaded supervisor required')
    try:
        settings = protocol(size=args.size, subscribers=args.subscribers, rates=args.rates,
                            samples=args.samples, warmup=args.warmup, repeats=args.repeats)
    except ValueError as error:
        parser.error(str(error))
    run_matrix(args.output, settings, matrix(settings), capture=identity,
               archive_sources=archive_sources, run_case=run_case, report=report, shuffle=False)


if __name__ == '__main__':
    main()
