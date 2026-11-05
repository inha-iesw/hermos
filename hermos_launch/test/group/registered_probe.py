#!/usr/bin/env python3
"""Direct metadata peer: collect real ROS settings only after registration ACK."""

import json
import os
from pathlib import Path
import signal
import sys
import time

import rclpy
from rclpy.node import Node
from rclpy.utilities import remove_ros_args
from hermos_runtime.supervision.registration import Registration


args = remove_ros_args(sys.argv)[1:]
rclpy.init(args=sys.argv[1:])
node = Node('unconfigured')
stopping = False


def stop(number, frame):
    global stopping
    stopping = True


if len(args) > 2:
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
try:
    Registration.from_environment(required=True).wait([])
    Path(args[0]).write_text(json.dumps(dict(
        name=node.get_name(), namespace=node.get_namespace(), topic=node.resolve_topic_name('image'),
        label=node.declare_parameter('label', 'missing').value, tag=args[1], registered=True,
        preload=os.environ.get('LD_PRELOAD'), scoped=os.environ.get('PROBE_SCOPED'),
        pid=os.getpid(), supervisor=os.getppid())))
    if len(args) > 2:
        while not stopping:
            time.sleep(0.01)
        time.sleep(float(args[2]))
        Path(args[0]).with_suffix('.drained').touch()
        if len(args) > 3:
            raise SystemExit(int(args[3]))
finally:
    node.destroy_node()
    rclpy.shutdown()
