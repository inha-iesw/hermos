"""Bounded synthetic GPU latency, without changing the adopted G4 contracts."""

import argparse
import itertools
import json
import os
from pathlib import Path
import sys

from experiments.g5.cpu_latency.analysis import analyze
from experiments.g5.cpu_latency.protocol import RMWS
from experiments.g5.cpu_latency.provenance import (
    ROOT, PREFIX, archive_sources, capture, digest, hash_artifacts, hash_sources,
    require_release, verify_peer)
from experiments.g5.harness import execute_epoch, peer_environment, run_matrix


SIZES = (1024, 4096, 65536, 1048576, 4194304)
GPU_PREFIX = ROOT / 'install/hermos/g5/gpu-comparison'
CORE_PREFIX = ROOT / 'install/hermos/g5/gpu-comparison-core'


def cases(compare_dds, sizes=SIZES):
    """Freeze matched descriptor transports; the original five-case mode remains available."""
    return [dict(size=size, rmw=rmw, descriptor_transport=transport,
                 name=f'{size}-{rmw}-{transport}' if compare_dds else str(size))
            for size, rmw, transport in itertools.product(
                sizes, RMWS if compare_dds else RMWS[:1],
                ('dds', 'hermos') if compare_dds else ('hermos',))]


def identity():
    value = capture()
    value['gpu_artifacts'] = {}
    for package, build, install in (
            ('ros2_cuda_ipc_msgs', 'gpu-comparison-core', CORE_PREFIX),
            ('ros2_cuda_ipc_core', 'gpu-comparison-core', CORE_PREFIX),
            ('rclpy_hermos_gpu_msgs', 'gpu-comparison-core', CORE_PREFIX),
            ('rclpy_hermos_gpu', 'gpu-comparison', GPU_PREFIX)):
        require_release(ROOT / 'build/hermos/g5' / build / package / 'CMakeCache.txt')
        value['gpu_artifacts'].update(hash_artifacts(install / package))
    required = [CORE_PREFIX / 'ros2_cuda_ipc_core/lib/libros2_cuda_ipc_core.a']
    required += [CORE_PREFIX / package / 'lib' /
                 f'lib{package}__rosidl_typesupport_fastrtps_{language}.so'
                 for package in ('ros2_cuda_ipc_msgs', 'rclpy_hermos_gpu_msgs')
                 for language in ('c', 'cpp')]
    for artifact in required:
        if not artifact.is_file():
            raise RuntimeError(f'missing GPU production artifact: {artifact}')
    value['source_sha256'].update(hash_sources((
        'rclpy_hermos_gpu', 'rclpy_hermos_gpu_msgs',
        'phase4_gpu_core_probe/repair/recovery', 'phase4_gpu_core_probe/repair/session',
        'build/g3/repaired/ros2_cuda_ipc/ros2_cuda_ipc_core',
        'build/g3/repaired/ros2_cuda_ipc/ros2_cuda_ipc_msgs')))
    patch = ROOT / 'phase4_gpu_core_probe/repair/patches/core-lifecycle.patch'
    value['canonical_patch_sha256'] = digest(patch)
    adopted_patch = 'e500ce18ffead8ad657739a3ec090ea2ca202aef540d4ec568acac52f88ac5f4'
    if value['canonical_patch_sha256'] != adopted_patch:
        raise RuntimeError('GPU core patch differs from the adopted input')
    return value


def make_config(case, protocol):
    """Validate the frozen protocol and derive every peer/runtime budget from it."""
    keys = ('blocks', 'max_reads', 'depth', 'descriptor_bytes', 'retained_bytes_per_read',
            'warmup', 'samples', 'period_ns')
    for key in keys:
        value = protocol.get(key)
        if type(value) is not int or value < (0 if key == 'warmup' else 1):
            raise ValueError(f'invalid {key}')
    if protocol.get('dtype') != 'uint8':
        raise ValueError('invalid dtype')
    if protocol['retained_bytes_per_read'] < protocol['descriptor_bytes']:
        raise ValueError('retained_bytes_per_read must cover descriptor_bytes')
    if (type(case.get('size')) is not int or case['size'] not in SIZES or
            case.get('rmw') not in RMWS or
            case.get('descriptor_transport') not in ('dds', 'hermos')):
        raise ValueError('invalid GPU case')
    return dict(case, **{key: protocol[key] for key in keys}, dtype=protocol['dtype'],
                max_retained_bytes=protocol['max_reads'] * protocol['retained_bytes_per_read'],
                topic=f'/g5/p{os.getpid()}/gpu')


def run_pair(config, path):
    path.mkdir()
    config_path = path / 'config.json'
    config_path.write_text(json.dumps(config, indent=2) + '\n')
    environment = peer_environment(
        config['rmw'], hermos=config['descriptor_transport'] == 'hermos',
        heaphook=PREFIX / 'hermoslib/lib/libhermos_heaphook.so')
    commands = []
    for role in ('publisher', 'subscriber'):
        argv = [sys.executable, '-u', '-m', 'rclpy_hermos_gpu.launch',
                '--blocks', str(config['blocks']) if role == 'publisher' else '0',
                '--block-bytes', str(config['size']), '--max-reads', str(config['max_reads']),
                '--max-message-bytes', str(config['size']),
                'experiments.g5.gpu_latency.peer:factory', role, str(config_path),
                str(path / f'{role}.json')]
        commands.append(dict(argv=argv, env=environment))
    execute_epoch(commands, path, expected=config['warmup'] + config['samples'],
                  period_ns=config['period_ns'], startup_timeout=60)
    publisher, subscriber = [json.loads((path / f'{role}.json').read_text())
                             for role in ('publisher', 'subscriber')]
    for record in (publisher, subscriber):
        verify_peer(record, config['descriptor_transport'] == 'hermos',
                    prefixes=(PREFIX, GPU_PREFIX, CORE_PREFIX))
        mapped = [Path(name) for name in record['loaded_objects']]
        if not any(p.is_relative_to(GPU_PREFIX) and p.name.startswith('_native.') for p in mapped):
            raise RuntimeError('GPU production native module missing')
        after = record['runtime_after']
        if after['active_owners'] or after['pending_owners']:
            raise RuntimeError(f'GPU wrapper owners not settled: {after}')
    if publisher['time_namespace'] != subscriber['time_namespace']:
        raise RuntimeError('GPU peers use different monotonic clock namespaces')
    result = analyze(publisher, subscriber, warmup=config['warmup'], samples=config['samples'])
    result['fill_to_gpu_done_ns'] = result.pop('fill_to_consumer_ns')
    completion = dict(samples=[[seq, done, done] for seq, callback, done in subscriber['samples']])
    completion_result = analyze(publisher, completion,
                                warmup=config['warmup'], samples=config['samples'])
    result['publish_to_gpu_done_ns'] = completion_result['publish_to_callback_ns']
    result['case'] = config
    (path / 'analysis.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def report(results, output, protocol=None):
    metrics = [('publish_to_callback_ns', 'mean'), ('publish_to_gpu_done_ns', 'mean'),
               ('publish_to_gpu_done_ns', 'p99'), ('publish_call_ns', 'mean'),
               ('fill_to_gpu_done_ns', 'mean'), ('fill_to_gpu_done_ns', 'p99')]
    lines = ['# Same-core GPU descriptor comparison', '',
             '| Bytes | RMW | Descriptor | Received/expected | Mean callback µs | '
             'Mean GPU-done µs | p99 GPU-done µs | Mean publish-call µs | '
             'Mean fill-to-GPU-done µs | p99 fill-to-GPU-done µs | Complete |',
             '|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---|']
    for result in results:
        case = result['case']
        values = [f"{result[metric][stat]/1000:.2f}" if result[metric] else 'N/A'
                  for metric, stat in metrics]
        lines.append(f"| {case['size']} | {case['rmw']} | {case['descriptor_transport']} | "
                     f"{result['received']}/{result['expected']} | " + ' | '.join(values) +
                     f" | {result['complete']} |")
    lines += ['', '## Matched mean publish-to-GPU-done change', '',
              'Positive percentages mean additional latency: (proposed / DDS - 1) × 100. '
              'Incomplete pairs are invalid.', '']
    controls = {(r['case']['size'], r['case']['rmw']): r for r in results
                if r['case']['descriptor_transport'] == 'dds'}
    for result in results:
        case = result['case']
        baseline = controls.get((case['size'], case['rmw']))
        # Historical evidence keeps its original transport identity.
        if case['descriptor_transport'] not in ('hermos', 'herm0s', 'agnocast') or baseline is None:
            continue
        current, reference = result['publish_to_gpu_done_ns'], baseline['publish_to_gpu_done_ns']
        if not result['complete'] or not baseline['complete']:
            change = 'INVALID (receipt loss)'
        elif not current or not reference or not reference['mean']:
            change = 'INVALID (unavailable or zero baseline)'
        else:
            change = f"{(current['mean'] / reference['mean'] - 1) * 100:+.2f}%"
        lines.append(f"- {case['size']} bytes, {case['rmw']}: {change}")
    lines += ['', 'Same CUDA core, pool, DLPack owners, Torch fill/checksum and stream protocol.',
              'The comparison includes descriptor API, serialization and heap integration costs; '
              'it is not a transport-only or host-staging comparison.',
              'Publish-call time is measured separately; it overlaps publish-to-callback and '
              'publish-to-GPU-done intervals and must not be added to them.',
              'Sixteen diagnostic DtoH bytes follow each timed GPU completion. '
              'No real VLN/rosbag or 10% acceptance verdict is established.', '']
    (output / 'report.md').write_text('\n'.join(lines))


def run_case(config, protocol, path):
    return run_pair(config, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--compare-dds', action='store_true',
                        help='matched same-core DDS/Hermos descriptor matrix across both RMWs')
    args = parser.parse_args()
    if not args.output.is_absolute() or os.environ.get('LD_PRELOAD'):
        parser.error('fresh absolute output and non-preloaded supervisor required')
    protocol = dict(sizes=list(SIZES), dtype='uint8', blocks=16, max_reads=16,
                    depth=16, descriptor_bytes=4096, retained_bytes_per_read=4096,
                    warmup=64, samples=16 if args.smoke else 1000, period_ns=20000000,
                    diagnostic_dtoh_bytes_per_sample=16,
                    compare_dds=args.compare_dds, order_seed=20261101,
                    requirement_verdict='not-assessed: synthetic, no real VLN')
    protocol['case_order'] = 'seeded-shuffle' if args.compare_dds else 'ascending-size'
    matrix = [make_config(case, protocol) for case in cases(args.compare_dds, protocol['sizes'])]
    run_matrix(args.output, protocol, matrix, capture=identity,
               archive_sources=archive_sources, run_case=run_case, report=report,
               shuffle=args.compare_dds)


if __name__ == '__main__':
    main()
