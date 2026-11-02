"""Explicit CPU copy relay between DDS and generated Hermos endpoints."""

import argparse
from array import array
import json
import os
from pathlib import Path
import select
import signal
import sys
import time

from experiments.g5.cpu_latency.peer import emit, register, snapshot, warmup_snapshot_due
from experiments.g5.cpu_latency.protocol import validate
from experiments.g5.cpu_latency.provenance import mapped_objects, require_uninstrumented


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.case.read_text())
    case, protocol = config['case'], config['protocol']
    validate(protocol)
    incoming, outgoing = case['input'], case['output']
    if (incoming, outgoing) not in (('dds', 'dds'), ('hermos', 'dds'), ('dds', 'hermos')):
        raise ValueError('unsupported relay path')
    import rclpy
    from g5_benchmark_msgs.msg import Dynamic
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from rclpy.signals import SignalHandlerOptions
    mixed = 'hermos' in (incoming, outgoing)
    if mixed:
        from rclpy_hermos import Node, SubscriptionLimits, _native
        require_uninstrumented(_native)
    else:
        from rclpy.node import Node
        if os.environ.get('LD_PRELOAD'):
            raise RuntimeError('DDS relay control must not preload Hermos')
    rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    node = Node(f'g5_relay_{os.getpid()}')
    qos = QoSProfile(depth=protocol['depth'], reliability=ReliabilityPolicy.BEST_EFFORT)
    limit = case['size'] + 4096
    rows = []
    stopped = finished = False
    report = dict(case=case, pid=os.getpid(), samples=rows,
                  time_namespace=os.readlink('/proc/self/ns/time'), explicit_payload_copy=True)
    executor = None

    def stop(signum, frame):
        nonlocal stopped
        stopped = True

    def callback(message):
        entered = time.monotonic_ns()
        sequence = message.sequence
        source = message.data.buffer() if incoming == 'hermos' else memoryview(message.data)
        try:
            if len(source) != case['size']:
                raise RuntimeError('relay payload length mismatch')
            if outgoing == 'hermos':
                with publisher.borrow_message() as output:
                    output.data.resize(case['size'])
                    output.sequence = sequence
                    with output.data.buffer() as destination:
                        destination[:] = source
                    source.release()
                    published = time.monotonic_ns()
                    publisher.publish(output)
                    returned = time.monotonic_ns()
            else:
                output = Dynamic()
                output.sequence = sequence
                output.data = array('B', [0]) * case['size']
                with memoryview(output.data) as destination:
                    destination[:] = source
                source.release()
                published = time.monotonic_ns()
                publisher.publish(output)
                returned = time.monotonic_ns()
        finally:
            source.release()
        rows.append([sequence, entered, published, returned])
        if warmup_snapshot_due(report, sequence, protocol['warmup']):
            report['resources_after_warmup'] = snapshot()
            report['warmup_snapshot_sequence'] = sequence

    try:
        options = (dict(transport='hermos', max_message_bytes=limit)
                   if outgoing == 'hermos' else {})
        publisher = node.create_publisher(Dynamic, config['output_topic'], qos, **options)
        options = dict(transport='hermos', limits=SubscriptionLimits(
            max_messages=protocol['depth'], max_retained_bytes=protocol['depth'] * limit,
            max_message_bytes=limit)) if incoming == 'hermos' else {}
        subscription = node.create_subscription(
            Dynamic, config['input_topic'], callback, qos, **options)
        executor = SingleThreadedExecutor(context=node.context)
        executor.add_node(node)
        # Endpoint construction installs native handlers; regain peer ownership afterward.
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        register()
        report['resources_before'] = snapshot()
        report['loaded_objects'] = mapped_objects()
        emit('READY', pid=os.getpid())
        pending = b''
        while not stopped:
            executor.spin_once(timeout_sec=0.005)
            if not select.select([sys.stdin], [], [], 0)[0]:
                continue
            chunk = os.read(sys.stdin.fileno(), 4096)
            if not chunk:
                stopped = True
                continue
            pending += chunk
            while b'\n' in pending:
                raw, pending = pending.split(b'\n', 1)
                command = raw.decode().strip()
                if command == 'connections':
                    count = (subscription.publisher_count if incoming == 'hermos' else
                             node.count_publishers(config['input_topic']))
                    emit('CONNECTED', count=count)
                elif command == 'count':
                    emit('COUNT', count=len(rows))
                elif command == 'finish' and not finished:
                    report['resources_after'] = snapshot()
                    with args.output.open('x') as output:
                        json.dump(report, output)
                    finished = True
                    emit('SAVED', count=len(rows))
                else:
                    raise ValueError(f'unexpected relay command: {command}')
        if not finished:
            emit('CANCELLED', count=len(rows))
    finally:
        if executor is not None:
            executor.shutdown(timeout_sec=3)
            executor.remove_node(node)
        node.destroy_node()
        rclpy.shutdown()
    emit('STOPPED')


if __name__ == '__main__':
    main()
