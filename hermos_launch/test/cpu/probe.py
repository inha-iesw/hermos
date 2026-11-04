"""Host-only stock ROS node used to observe launch's actual argv and environment."""

import argparse
import json
import os
from pathlib import Path
import sys

import rclpy
from rclpy.node import Node
from rclpy.utilities import remove_ros_args

parser = argparse.ArgumentParser()
parser.add_argument('--output', required=True)
parser.add_argument('--tag', required=True)
args = parser.parse_args(remove_ros_args(sys.argv)[1:])
rclpy.init(args=sys.argv[1:])
node = Node('unconfigured')
try:
    label = node.declare_parameter('label', 'missing').value
    Path(args.output).write_text(json.dumps(dict(
        name=node.get_name(), namespace=node.get_namespace(),
        topic=node.resolve_topic_name('image'), label=label, tag=args.tag,
        argv=sys.argv, preload=os.environ.get('LD_PRELOAD'),
        device=os.environ.get('HERMOS_DEVICE_PATH'), scoped=os.environ.get('PROBE_SCOPED'))))
finally:
    node.destroy_node()
    rclpy.shutdown()
