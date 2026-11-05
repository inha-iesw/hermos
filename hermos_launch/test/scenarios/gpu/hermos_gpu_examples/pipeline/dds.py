"""An ordinary DDS Node outside the supervised GPU namespace."""

import argparse
import os
from pathlib import Path
import signal
import sys
import time

from .peer import node_proof, write_proof


def main():
    """Exchange DDS status until the finite GPU writer records successful completion."""
    import rclpy
    from rclpy.context import Context
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.node import Node
    from rclpy.signals import SignalHandlerOptions
    from rclpy.utilities import remove_ros_args
    from std_msgs.msg import String

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output_directory', type=Path)
    arguments = parser.parse_args(remove_ros_args(sys.argv)[1:])
    context = Context()
    stopped = []
    previous = {number: signal.signal(number, lambda number, frame: stopped.append(number))
                for number in (signal.SIGINT, signal.SIGTERM)}
    rclpy.init(args=sys.argv[1:], context=context, signal_handler_options=SignalHandlerOptions.NO)
    node = Node('dds_factory', context=context)
    node.declare_parameter('label', '')
    publisher = node.create_publisher(String, 'outside_status', 10)

    def receive(message):
        if message.data != 'inside DDS ready':
            raise ValueError('unexpected inside DDS payload')
        write_proof(arguments.output_directory / 'outside.json',
                    dict(**node_proof(node, 'inside_status'), received=message.data,
                         preload=os.environ.get('LD_PRELOAD', '')))

    node.create_subscription(String, 'inside_status', receive, 10)
    node.create_timer(0.02, lambda: publisher.publish(String(data='outside DDS ready')))
    executor = SingleThreadedExecutor(context=context)
    executor.add_node(node)
    deadline = time.monotonic() + 90
    try:
        while not stopped and not (arguments.output_directory / 'writer.json').exists():
            if time.monotonic() > deadline:
                raise RuntimeError('outside DDS peer did not observe GPU completion')
            executor.spin_once(timeout_sec=0.02)
    finally:
        executor.shutdown()
        node.destroy_node()
        context.shutdown()
        for number, handler in previous.items():
            signal.signal(number, handler)
    return 0
