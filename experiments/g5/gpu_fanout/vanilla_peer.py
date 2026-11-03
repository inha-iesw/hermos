"""Pageable GPU→host→stock DDS→GPU baseline with the same ROS timer pacing."""

import argparse
from array import array
import json
import os
from pathlib import Path
import select
import signal
import sys
import time


def validate_checksum(sequence, actual, *, size, total, message_sequence):
    """Require both the in-payload sequence and checksum over every uint8 element."""
    if sequence < 0 or sequence >= total or sequence != message_sequence:
        raise RuntimeError(f'GPU sequence is outside or differs from the message: {sequence}')
    expected = (size - 8) * (sequence % 251) + sum(sequence.to_bytes(8, 'little'))
    if actual != expected:
        raise RuntimeError(f'GPU checksum mismatch for sequence {sequence}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--role', required=True, choices=('publisher', 'subscriber'))
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if not args.config.is_absolute() or not args.output.is_absolute() or os.environ.get('LD_PRELOAD'):
        parser.error('absolute paths and a non-preloaded stock DDS peer are required')
    config = json.loads(args.config.read_text())
    for key, minimum in (('size', 1024), ('samples', 1), ('warmup', 0), ('period_ns', 1)):
        if type(config[key]) is not int or config[key] < minimum:
            raise ValueError(f'invalid {key}')
    if config['transport'] != 'vanilla' or config['dtype'] != 'uint8':
        raise ValueError('expected vanilla uint8 configuration')

    import torch
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from rclpy.signals import SignalHandlerOptions
    from g5_benchmark_msgs.msg import Dynamic
    from experiments.g5.cpu_latency.peer import emit, observe_sample_boundary, register, snapshot
    from experiments.g5.cpu_latency.provenance import mapped_objects

    size = config['size']
    total = config['warmup'] + config['samples']
    if total >= 2**63:
        raise ValueError('sequence must fit nonnegative int64')
    torch.cuda.set_device(0)
    stream = torch.cuda.Stream(device=0)
    completion = torch.cuda.Event(enable_timing=False) if args.role == 'subscriber' else None
    rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    node = Node(f'g5_fanout_vanilla_{args.role}_{os.getpid()}')
    executor = SingleThreadedExecutor(context=node.context)
    executor.add_node(node)
    qos = QoSProfile(depth=config['depth'], reliability=ReliabilityPolicy.BEST_EFFORT)
    records = []
    report = dict(config=config, role=args.role, pid=os.getpid(), samples=records,
                  resource_diagnostics=False, time_namespace=os.readlink('/proc/self/ns/time'),
                  diagnostic_dtoh_bytes_per_sample=16 if args.role == 'subscriber' else 0,
                  torch_version=torch.__version__, cuda_version=torch.version.cuda,
                  device_name=torch.cuda.get_device_name(0))
    ready = started = finished = stopped = False
    pending = b''

    def stop(signum, frame):
        nonlocal stopped
        stopped = True

    def boundary(sequence):
        observe_sample_boundary(report, sequence, warmup=config['warmup'], samples=config['samples'])

    def callback(message):
        entered = time.monotonic_ns()
        host = torch.frombuffer(message.data, dtype=torch.uint8)
        if host.numel() != size:
            raise RuntimeError('unexpected GPU payload layout')
        with torch.cuda.stream(stream):
            tensor = host.to('cuda:0')
            checksum = tensor.sum(dtype=torch.int64)
            completion.record(stream)
        completion.synchronize()
        gpu_done = time.monotonic_ns()
        # Match the IPC peer: two scalar reads occur after timed GPU completion.
        with torch.cuda.stream(stream):
            header = tensor[:8].view(torch.int64)
            sequence = int(header.item())
            actual = int(checksum.item())
        validate_checksum(sequence, actual, size=size, total=total, message_sequence=message.sequence)
        del header, checksum, tensor, host
        records.append([sequence, entered, gpu_done])
        boundary(sequence)

    if args.role == 'publisher':
        device = torch.empty(size, dtype=torch.uint8, device='cuda:0')
        endpoint = node.create_publisher(Dynamic, config['topic'], qos)
    else:
        endpoint = node.create_subscription(Dynamic, config['topic'], callback, qos)

    def publish_one():
        sequence = len(records)
        fill_started = time.monotonic_ns()
        with torch.cuda.stream(stream):
            device.fill_(sequence % 251)
            device[:8].view(torch.int64).fill_(sequence)
            host = device.cpu()
        message = Dynamic()
        message.sequence = sequence
        data = array('B')
        data.frombytes(host.numpy())
        message.data = data
        published = time.monotonic_ns()
        endpoint.publish(message)
        returned = time.monotonic_ns()
        records.append([sequence, fill_started, published, returned])
        boundary(sequence)
        if len(records) == total:
            emit('SENT', count=len(records))

    def commands():
        nonlocal ready, started, finished, pending
        if not ready:
            report['resources_before'] = snapshot()
            report['loaded_objects'] = mapped_objects()
            ready = True
            emit('READY', pid=os.getpid())
        if select.select([sys.stdin], [], [], 0)[0]:
            chunk = os.read(sys.stdin.fileno(), 4096)
            if not chunk:
                raise RuntimeError('measurement control closed before supervisor shutdown')
            pending += chunk
        while b'\n' in pending:
            raw, pending = pending.split(b'\n', 1)
            command = raw.decode().strip()
            if command == 'start' and args.role == 'publisher' and not started and not finished:
                started = True
            elif command == 'connections' and args.role == 'subscriber' and not finished:
                emit('CONNECTED', count=node.count_publishers(config['topic']))
            elif command == 'count' and args.role == 'subscriber' and not finished:
                emit('COUNT', count=len(records))
            elif command == 'finish' and not finished:
                if args.role == 'publisher' and len(records) != total:
                    raise RuntimeError('cannot finish an incomplete publication run')
                report['resources_after'] = snapshot()
                with args.output.open('x') as output:
                    json.dump(report, output, indent=2)
                finished = True
                emit('SAVED', count=len(records))
            else:
                raise ValueError(f'unexpected {args.role} command: {command}')
        if args.role == 'publisher' and started and not finished and len(records) < total:
            publish_one()

    node.create_timer(config['period_ns'] / 1e9 if args.role == 'publisher' else .005, commands)
    try:
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        register()
        while not stopped:
            executor.spin_once(timeout_sec=.05)
        if not finished:
            raise RuntimeError('measurement interrupted before reports were saved')
    finally:
        executor.shutdown(timeout_sec=3)
        executor.remove_node(node)
        node.destroy_node()
        rclpy.shutdown()
    emit('STOPPED')


if __name__ == '__main__':
    main()
