"""Send or validate six small Images through the installed CPU wrapper."""

import argparse
import json
import os
import signal
import sys
import time

import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import QoSProfile, ReliabilityPolicy
from rclpy.signals import SignalHandlerOptions
from rclpy.utilities import remove_ros_args
from rclpy_hermos import Node, SubscriptionLimits
from sensor_msgs.msg import Image
from std_msgs.msg import String

from hermos_runtime.environment.process import loaded_paths, malloc_provider


def emit(event, **fields):
    """Keep example evidence machine-readable alongside ROS logging."""
    print('HERMOS_EXAMPLE ' + json.dumps(dict(event=event, **fields), ensure_ascii=False), flush=True)


def main():
    """Own ROS setup and orderly endpoint cleanup after the wrapper prepared malloc."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=('publisher', 'subscriber'), required=True)
    parser.add_argument('--count', type=int, default=6)
    parser.add_argument('--start-delay', type=float, default=1.0)
    parser.add_argument('--timeout', type=float, default=15.0)
    parser.add_argument('--tag', default='CPU example')
    args = parser.parse_args(remove_ros_args(sys.argv)[1:])
    if args.count < 1 or args.timeout <= 0 or args.start_delay < 0:
        parser.error('count/timeout must be positive and start-delay nonnegative')
    stopped = []
    old_handlers = {}
    rclpy.init(args=sys.argv[1:], signal_handler_options=SignalHandlerOptions.NO)
    node = Node('image_' + args.role)
    executor = SingleThreadedExecutor(context=node.context)
    executor.add_node(node)
    count = 0
    dds_received = []
    try:
        label = node.declare_parameter('label', 'CPU example label').value
        qos = QoSProfile(depth=8, reliability=ReliabilityPolicy.BEST_EFFORT)
        # DDS and parameter services coexist with the explicitly selected native endpoint.
        dds_publisher = node.create_publisher(String, 'status', 8)
        node.create_subscription(String, 'status', lambda message: dds_received.append(message.data), 8)

        def receive(message):
            nonlocal count
            if (message.header.stamp.sec != count or message.width != 64 or message.height != 1
                    or message.step != 64 or message.encoding.to_text() != 'mono8'):
                raise ValueError('unexpected Image metadata or sequence')
            with message.data.buffer() as data:
                if bytes(data) != bytes((count + index) % 256 for index in range(64)):
                    raise ValueError('Image payload mismatch')
            count += 1
            emit('received', count=count)

        if args.role == 'subscriber':
            node.create_subscription(Image, 'image', receive, qos, transport='hermos',
                limits=SubscriptionLimits(max_messages=8, max_retained_bytes=524288,
                                          max_message_bytes=65536))
            publisher = None
        else:
            publisher = node.create_publisher(Image, 'image', qos, transport='hermos',
                                               max_message_bytes=65536)
        # Native endpoint initialization installs its own chained handlers. The
        # example takes signal ownership afterwards so the main loop can close
        # the live executor before either context is invalidated asynchronously.
        old_handlers = {kind: signal.signal(kind, lambda number, frame: stopped.append(number))
                        for kind in (signal.SIGINT, signal.SIGTERM)}
        emit('ready', pid=os.getpid(), argv=sys.argv, role=args.role,
             name=node.get_name(), namespace=node.get_namespace(),
             topic=node.resolve_topic_name('image'), label=label, tag=args.tag,
             malloc_provider=str(malloc_provider()),
             loaded=[str(path) for path in sorted(loaded_paths())
                     if any('hermos' in part for part in path.parts)
                     or path.name.startswith('_native.')])
        started = time.monotonic()
        next_publish = started + args.start_delay
        status = String(data=args.tag)
        while count < args.count and not stopped and time.monotonic() - started < args.timeout:
            executor.spin_once(timeout_sec=0.01)
            now = time.monotonic()
            dds_publisher.publish(status)
            if publisher is not None and now >= next_publish:
                with publisher.borrow_message() as message:
                    message.encoding.resize(5)
                    message.data.resize(64)
                    message.header.stamp.sec = count
                    message.width, message.height, message.step = 64, 1, 64
                    message.is_bigendian = 0
                    with message.encoding.buffer() as encoding:
                        encoding[:] = b'mono8'
                    with message.data.buffer() as data:
                        data[:] = bytes((count + index) % 256 for index in range(64))
                    publisher.publish(message)
                count += 1
                next_publish = now + 0.05
                emit('published', count=count)
        if count != args.count and not stopped:
            raise RuntimeError(f'Image timeout: {count}/{args.count}')
        # A short drain permits the final DDS callback without retaining native aliases.
        if not stopped:
            executor.spin_once(timeout_sec=0.05)
        emit('done', role=args.role, count=count, expected=args.count,
             signal=stopped[-1] if stopped else None, dds_received=len(dds_received))
    finally:
        executor.remove_node(node)
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()
        for kind, handler in old_handlers.items():
            signal.signal(kind, handler)
    emit('closed', pid=os.getpid(), role=args.role)
    return 0
