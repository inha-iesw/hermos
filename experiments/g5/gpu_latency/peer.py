"""Bounded GPU timing peers hosted by the existing registered G4 launcher."""

import json
import importlib
import os
from pathlib import Path
import select
import sys
import time

import torch
from rclpy.qos import QoSProfile, ReliabilityPolicy

from experiments.g5.cpu_latency.peer import emit, snapshot, warmup_snapshot_due
from experiments.g5.cpu_latency.provenance import mapped_objects, require_uninstrumented
from rclpy_hermos_gpu_msgs.msg import GpuTensor


def factory(runtime, arguments):
    """Create one peer; registration, execution and teardown belong to the launcher.

    Arguments are role, configuration JSON path and fresh result JSON path.
    Consumer completion ends at a synchronized read-only GPU checksum. Two scalar
    DtoH diagnostics follow that timestamp and are excluded from its duration.
    Their 16 bytes per publication are explicit and are not payload staging.
    """
    if len(arguments) != 3 or arguments[0] not in ('publisher', 'subscriber'):
        raise ValueError('expected publisher/subscriber, configuration path, output path')
    if not all(os.environ.get(key) for key in ('G3_RECOVERY_MANIFEST', 'G3_RECOVERY_ACK')):
        raise RuntimeError('GPU benchmark requires its registered supervisor')
    role, config_name, output_name = arguments
    config_path, output_path = Path(config_name), Path(output_name)
    if not config_path.is_absolute() or not output_path.is_absolute():
        raise ValueError('configuration and output paths must be absolute')
    config = json.loads(config_path.read_text())
    transport = config['descriptor_transport']
    require_uninstrumented(importlib.import_module('rclpy_hermos_gpu._native'))
    if transport == 'dds':
        from rclpy.node import Node
        from .dds_endpoint import DDSPublisher, DDSSubscription
        if os.environ.get('LD_PRELOAD'):
            raise RuntimeError('DDS descriptor baseline must not preload Hermos')
    elif transport == 'hermos':
        from rclpy_hermos import Node, SubscriptionLimits, _native
        require_uninstrumented(_native)
    else:
        raise ValueError('unsupported GPU descriptor transport')
    for key, minimum in (('size', 1024), ('samples', 1), ('warmup', 0), ('period_ns', 1)):
        if type(config[key]) is not int or config[key] < minimum:
            raise ValueError(f'{key} must be an integer >= {minimum}')
    total_count = config['warmup'] + config['samples']
    if total_count >= 2**63:
        raise ValueError('sequence must fit a nonnegative int64')
    size = config['size']
    node = Node(f'g5_gpu_{role}_{os.getpid()}', context=runtime.ros_context)
    stream = torch.cuda.Stream(device=runtime.device)
    completion = torch.cuda.Event(enable_timing=False) if role == 'subscriber' else None
    # These caller-owned objects must outlive every pending owner during shutdown.
    node.gpu_benchmark_stream = stream
    node.gpu_benchmark_completion = completion
    qos = QoSProfile(depth=config['depth'], reliability=ReliabilityPolicy.BEST_EFFORT)
    records = []
    report = dict(config=config, role=role, pid=os.getpid(), samples=records,
                  time_namespace=os.readlink('/proc/self/ns/time'),
                  diagnostic_dtoh_bytes_per_sample=16 if role == 'subscriber' else 0)
    ready = started = finished = False
    pending = b''

    def warmup_snapshot(sequence):
        if warmup_snapshot_due(report, sequence, config['warmup']):
            runtime.poll()
            report['resources_after_warmup'] = snapshot()
            report['runtime_after_warmup'] = runtime.stats
            report['warmup_snapshot_sequence'] = sequence

    def callback(lease):
        entered = time.monotonic_ns()
        tensor = lease.as_tensor(stream=stream)
        if tuple(tensor.shape) != (size,) or tensor.dtype != torch.uint8:
            raise RuntimeError('unexpected GPU payload layout')
        with torch.cuda.stream(stream):
            checksum = tensor.sum(dtype=torch.int64)
            completion.record(stream)
        completion.synchronize()
        gpu_done = time.monotonic_ns()
        # Only these scalar diagnostics copy to the host, after the timed GPU read.
        with torch.cuda.stream(stream):
            header = tensor[:8].view(torch.int64)
            sequence = int(header.item())
            actual_sum = int(checksum.item())
        if sequence < 0 or sequence >= total_count:
            raise RuntimeError('GPU sequence is outside the scheduled range')
        expected_sum = ((size - 8) * (sequence % 251) +
                        sum(sequence.to_bytes(8, 'little')))
        if actual_sum != expected_sum:
            raise RuntimeError(f'GPU checksum mismatch for sequence {sequence}')
        del header, checksum, tensor
        records.append([sequence, entered, gpu_done])
        warmup_snapshot(sequence)

    if transport == 'dds':
        endpoint = (DDSPublisher(node, config['topic'], qos, runtime) if role == 'publisher' else
                    DDSSubscription(node, config['topic'], callback, qos, runtime))
    elif role == 'publisher':
        endpoint = node.create_publisher(
            GpuTensor, config['topic'], qos, transport='hermos',
            max_message_bytes=config['descriptor_bytes'])
    else:
        endpoint = node.create_subscription(
            GpuTensor, config['topic'], callback, qos, transport='hermos',
            limits=SubscriptionLimits(config['max_reads'], config['max_retained_bytes'],
                                      config['descriptor_bytes']))

    def publish_one():
        sequence = len(records)
        fill_started = time.monotonic_ns()
        with endpoint.borrow_tensor((size,), torch.uint8, f'cuda:{runtime.device}', stream) as loan:
            tensor = loan.as_tensor()
            with torch.cuda.stream(stream):
                tensor.fill_(sequence % 251)
                header = tensor[:8].view(torch.int64)
                header.fill_(sequence)
            # Both views own writable storage; public publish rejects surviving aliases.
            del header, tensor
            published = time.monotonic_ns()
            endpoint.publish(loan)
            returned = time.monotonic_ns()
        records.append([sequence, fill_started, published, returned])
        warmup_snapshot(sequence)
        if len(records) == total_count:
            emit('SENT', count=len(records))

    def commands():
        nonlocal ready, started, finished, pending
        if not runtime.registered:
            raise RuntimeError('GPU dispatch started before supervisor registration')
        if not ready:
            report['resources_before'] = snapshot()
            report['runtime_before'] = runtime.stats
            report['torch_version'] = torch.__version__
            report['cuda_version'] = torch.version.cuda
            report['device_name'] = torch.cuda.get_device_name(runtime.device)
            report['loaded_objects'] = mapped_objects()
            ready = True
            emit('READY', pid=os.getpid())
        if select.select([sys.stdin], [], [], 0)[0]:
            chunk = os.read(sys.stdin.fileno(), 4096)
            if not chunk:
                raise RuntimeError('GPU measurement control closed before launcher shutdown')
            pending += chunk
        while b'\n' in pending:
            raw, pending = pending.split(b'\n', 1)
            command = raw.decode().strip()
            if command == 'start' and role == 'publisher' and not started and not finished:
                started = True
            elif command == 'connections' and role == 'subscriber' and not finished:
                emit('CONNECTED', count=endpoint.publisher_count)
            elif command == 'count' and role == 'subscriber' and not finished:
                emit('COUNT', count=len(records))
            elif command == 'finish' and not finished:
                if role == 'publisher' and len(records) != total_count:
                    raise RuntimeError('cannot finish an incomplete GPU publication run')
                runtime.poll()
                report['resources_after'] = snapshot()
                report['runtime_after'] = runtime.stats
                with output_path.open('x') as output:
                    json.dump(report, output, indent=2)
                finished = True
                emit('SAVED', count=len(records))
            else:
                raise ValueError(f'unexpected {role} command: {command}')
        if role == 'publisher' and started and not finished and len(records) < total_count:
            publish_one()

    node.create_timer(config['period_ns'] / 1e9 if role == 'publisher' else 0.005, commands)
    return node
