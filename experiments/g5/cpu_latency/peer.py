"""Direct registered CPU peers; measurement runs without parent per-message ACKs."""

import argparse
from array import array
import json
import math
import os
from pathlib import Path
import select
import resource
import signal
import sys
import time

from .provenance import mapped_objects, require_uninstrumented


def emit(event, **values):
    print(json.dumps(dict(event=event, **values)), flush=True)


def snapshot(*, network=False):
    """Observe process counters outside the timed publication sequence."""
    fields = {}
    for line in Path('/proc/self/status').read_text().splitlines():
        key, _, value = line.partition(':')
        if key in ('VmRSS', 'VmHWM', 'VmSize', 'Threads'):
            fields[key] = int(value.split()[0])
    fields['fds'] = len(list(Path('/proc/self/fd').iterdir()))
    usage = resource.getrusage(resource.RUSAGE_SELF)
    fields.update(cpu_user_seconds=usage.ru_utime, cpu_system_seconds=usage.ru_stime,
                  voluntary_context_switches=usage.ru_nvcsw,
                  involuntary_context_switches=usage.ru_nivcsw)
    if network:
        from .observation import owned_udp_sockets
        fields['udp_sockets'] = owned_udp_sockets()
    return fields


def register():
    """Require the GPU runtime's registration handshake for a benchmark-only empty inventory.

    Keep path/timeout semantics aligned with rclpy_hermos_gpu.runtime.register.
    This is not a standalone or general ROS CPU launcher.
    """
    manifest = Path(os.environ['G3_RECOVERY_MANIFEST'])
    ack = Path(os.environ['G3_RECOVERY_ACK'])
    temporary = Path(str(manifest) + '.tmp')
    if not manifest.is_absolute() or not ack.is_absolute():
        raise RuntimeError('registration paths must be absolute')
    if manifest.exists() or temporary.exists() or ack.exists():
        raise RuntimeError('registration needs a new epoch')
    timeout = float(os.environ.get('G3_RECOVERY_START_TIMEOUT', '10'))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('recovery startup timeout must be finite and positive')
    with temporary.open('x') as output:
        json.dump(dict(version=1, pid=os.getpid(), resources=[]), output)
        output.write('\n')
    temporary.replace(manifest)
    deadline = time.monotonic() + timeout
    while not ack.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('registration acknowledgement')
        time.sleep(0.005)


def warmup_snapshot_due(report, sequence, warmup):
    """Allow the next observed sequence to supply a missing warm-up boundary snapshot."""
    return 'resources_after_warmup' not in report and sequence >= max(0, warmup - 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', required=True, type=Path)
    parser.add_argument('--role', required=True, choices=('publisher', 'subscriber'))
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    config = json.loads(args.case.read_text())
    case, protocol = config['case'], config['protocol']
    from .protocol import validate
    validate(protocol)
    import numpy as np
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from rclpy.signals import SignalHandlerOptions
    import g5_benchmark_msgs.msg as messages

    if case['transport'] not in ('hermos', 'dds'):
        raise ValueError('unsupported CPU transport')
    proposed = case['transport'] == 'hermos'
    if proposed:
        from rclpy_hermos import Node, SubscriptionLimits, _native
        require_uninstrumented(_native)
    else:
        from rclpy.node import Node
        if os.environ.get('LD_PRELOAD'):
            raise RuntimeError('DDS baseline must not preload Hermos')
    message_type = getattr(messages, case['type'])
    rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    node = Node(f'g5_{args.role}_{os.getpid()}')
    qos = QoSProfile(depth=protocol['depth'], reliability=ReliabilityPolicy.BEST_EFFORT)
    limit = case['size'] + 4096
    records = []
    stopped = False
    finished = False
    started = False
    executor = None
    report = dict(case=case, pid=os.getpid(), samples=records,
                  time_namespace=os.readlink('/proc/self/ns/time'))

    def stop(signum, frame):
        nonlocal stopped
        stopped = True

    def callback(message):
        entered = time.monotonic_ns()
        sequence = message.sequence
        source = message.data.buffer() if proposed else memoryview(message.data)
        try:
            if len(source) != case['size']:
                raise RuntimeError('payload length mismatch')
            expected = sequence % 251
            if source[0] != expected or source[-1] != expected:
                raise RuntimeError('payload endpoint mismatch')
            if protocol['full_check'] and not np.all(np.frombuffer(source, np.uint8) == expected):
                raise RuntimeError('full payload mismatch')
        finally:
            source.release()
        completed = time.monotonic_ns()
        records.append([sequence, entered, completed])
        if warmup_snapshot_due(report, sequence, protocol['warmup']):
            report['resources_after_warmup'] = snapshot(
                network=protocol.get('network_diagnostics', False))
            report['warmup_snapshot_sequence'] = sequence

    try:
        if args.role == 'publisher':
            options = dict(transport='hermos', max_message_bytes=limit) if proposed else {}
            endpoint = node.create_publisher(message_type, config['topic'], qos, **options)
        else:
            options = dict(transport='hermos', limits=SubscriptionLimits(
                max_messages=protocol['depth'], max_retained_bytes=protocol['depth'] * limit,
                max_message_bytes=limit)) if proposed else {}
            endpoint = node.create_subscription(
                message_type, config['topic'], callback, qos, **options)
            executor = SingleThreadedExecutor(context=node.context)
            executor.add_node(node)
        # Native Hermos setup installs handlers. Restore ownership after endpoint creation.
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        register()
        report['resources_before'] = snapshot(network=protocol.get('network_diagnostics', False))
        report['loaded_objects'] = mapped_objects()
        emit('READY', pid=os.getpid())
        pending = b''
        while not stopped:
            if executor is not None:
                executor.spin_once(timeout_sec=0.005)
            readable, _, _ = select.select([sys.stdin], [], [], 0 if executor else 0.02)
            if not readable:
                continue
            chunk = os.read(sys.stdin.fileno(), 4096)
            if not chunk:
                stopped = True
                continue
            pending += chunk
            while b'\n' in pending:
                raw, pending = pending.split(b'\n', 1)
                command = raw.decode().strip()
                if command == 'connections' and args.role == 'subscriber':
                    count = (endpoint.publisher_count if proposed else
                             node.count_publishers(config['topic']))
                    emit('CONNECTED', count=count)
                elif command == 'count' and args.role == 'subscriber':
                    emit('COUNT', count=len(records))
                elif command == 'start' and args.role == 'publisher' and not started:
                    started = True
                    start = time.monotonic_ns()
                    for sequence in range(protocol['warmup'] + protocol['samples']):
                        if stopped:
                            raise RuntimeError('measurement interrupted')
                        remaining = (
                            start + sequence * protocol['period_ns'] - time.monotonic_ns()) / 1e9
                        if remaining > 0:
                            time.sleep(remaining)
                        fill_started = time.monotonic_ns()
                        if proposed:
                            with endpoint.borrow_message() as message:
                                if case['layout'] == 'dynamic':
                                    message.data.resize(case['size'])
                                message.sequence = sequence
                                with message.data.buffer() as destination:
                                    np.frombuffer(destination, np.uint8).fill(sequence % 251)
                                published = time.monotonic_ns()
                                endpoint.publish(message)
                                returned = time.monotonic_ns()
                        else:
                            message = message_type()
                            message.sequence = sequence
                            if case['layout'] == 'dynamic':
                                message.data = array('B', [sequence % 251]) * case['size']
                            else:
                                message.data.fill(sequence % 251)
                            published = time.monotonic_ns()
                            endpoint.publish(message)
                            returned = time.monotonic_ns()
                        records.append([sequence, fill_started, published, returned])
                        if warmup_snapshot_due(report, sequence, protocol['warmup']):
                            report['resources_after_warmup'] = snapshot(
                                network=protocol.get('network_diagnostics', False))
                            report['warmup_snapshot_sequence'] = sequence
                    emit('SENT', count=len(records))
                elif command == 'finish' and not finished:
                    report['resources_after'] = snapshot(
                        network=protocol.get('network_diagnostics', False))
                    with args.output.open('x') as output:
                        json.dump(report, output)
                    finished = True
                    emit('SAVED', count=len(records))
                else:
                    raise ValueError(f'unexpected {args.role} command: {command}')
        if not finished:
            # Early TERM is a successful drain, but is not a successful measurement.
            emit('CANCELLED', count=len(records))
    finally:
        if executor is not None:
            executor.shutdown(timeout_sec=3)
            executor.remove_node(node)
        node.destroy_node()
        rclpy.shutdown()
    emit('STOPPED')


if __name__ == '__main__':
    main()
