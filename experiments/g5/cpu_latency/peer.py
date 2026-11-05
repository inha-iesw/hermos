"""Direct registered CPU peers; measurement runs without parent per-message ACKs."""

import argparse
from array import array
import json
import os
from pathlib import Path
import select
import resource
import signal
import sys
import time

from .provenance import mapped_objects, require_uninstrumented
from .observation import cpu_frequency_snapshot, memory_snapshot, thread_snapshot


def emit(event, **values):
    print(json.dumps(dict(event=event, **values)), flush=True)


def usage_snapshot():
    """Cumulative process counters with their observation timestamp."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return dict(cpu_user_seconds=usage.ru_utime, cpu_system_seconds=usage.ru_stime,
                voluntary_context_switches=usage.ru_nvcsw,
                involuntary_context_switches=usage.ru_nivcsw,
                minor_page_faults=usage.ru_minflt, major_page_faults=usage.ru_majflt,
                usage_collected_ns=time.monotonic_ns())


def snapshot(*, network=False, detailed=True):
    """Observe outside timed samples, bracketing the collection's own resource cost.

    A window starts at its first snapshot's post-collection counters and ends at
    its last snapshot's top-level counters. Scheduling disturbance from collecting
    snapshots is still possible; the collection interval is retained explicitly.
    Detailed memory/thread/frequency reads belong before/after the run or to
    explicitly separate diagnostic runs, because they can exceed a sample period.
    """
    fields = dict(collection_started_ns=time.monotonic_ns(), detailed=detailed)
    fields.update(usage_snapshot())
    for line in Path('/proc/self/status').read_text().splitlines():
        key, _, value = line.partition(':')
        if key in ('VmRSS', 'VmHWM', 'VmSize', 'Threads'):
            fields[key] = int(value.split()[0])
    fields['fds'] = len(list(Path('/proc/self/fd').iterdir()))
    fields.update(memory=None, cpu_frequency=None, threads=None)
    if detailed:
        fields['memory'] = memory_snapshot()
        fields['cpu_frequency'] = cpu_frequency_snapshot()
        try:
            fields['threads'] = thread_snapshot()
        except OSError as error:
            fields.update(threads=None, threads_error=str(error))
    if network:
        from .observation import owned_udp_sockets
        fields['udp_sockets'] = owned_udp_sockets()
    fields['post_collection_usage'] = usage_snapshot()
    fields['collection_finished_ns'] = time.monotonic_ns()
    return fields


def register():
    """Register the benchmark's empty inventory before dispatch begins."""
    from hermos_runtime.supervision.registration import Registration

    Registration.from_environment(required=True).wait([])


def warmup_snapshot_due(report, sequence, warmup):
    """Allow the next observed sequence to supply a missing warm-up boundary snapshot."""
    return 'resources_after_warmup' not in report and sequence >= max(0, warmup - 1)


def observe_sample_boundary(report, sequence, *, warmup, samples, network=False,
                            resource_diagnostics=False):
    """Keep measured completion separate from later drain/control-loop work.

    A terminal sequence can arrive before earlier messages. Counts describe the
    rows present at capture, and late receipts never rewrite that observation.
    An absent terminal receipt leaves the measurement-end snapshot unavailable.
    """
    boundaries = []
    if warmup_snapshot_due(report, sequence, warmup):
        boundaries.append(('after_warmup', 'warmup'))
    if sequence == warmup + samples - 1 and 'resources_measurement_end' not in report:
        boundaries.append(('measurement_end', 'measurement_end'))
    for resource_name, boundary in boundaries:
        report['resources_' + resource_name] = snapshot(
            network=network, detailed=resource_diagnostics)
        report[boundary + '_snapshot_sequence'] = sequence
        report[boundary + '_snapshot_count'] = len(report['samples'])
        measured = [row[0] for row in report['samples'] if row[0] >= warmup]
        report[boundary + '_measured_count'] = len(measured)
        if boundary == 'measurement_end':
            report['measurement_end_complete'] = (
                len(measured) == samples and set(measured) == set(range(warmup, warmup + samples)))
    return [resource_name for resource_name, _ in boundaries]


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
                  resource_diagnostics=protocol.get('resource_diagnostics', False),
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
        observe_sample_boundary(report, sequence, warmup=protocol['warmup'],
                                samples=protocol['samples'],
                                network=protocol.get('network_diagnostics', False),
                                resource_diagnostics=protocol.get('resource_diagnostics', False))

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
                        observe_sample_boundary(report, sequence, warmup=protocol['warmup'],
                                                samples=protocol['samples'],
                                                network=protocol.get('network_diagnostics', False),
                                                resource_diagnostics=protocol.get('resource_diagnostics', False))
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
