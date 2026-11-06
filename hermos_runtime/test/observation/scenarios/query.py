"""A late external observer using only the installed metadata collection API."""

import json
import hashlib
import os
from pathlib import Path
import sys

import rclpy
from rosidl_runtime_py.convert import message_to_ordereddict
from ros2hermos.discovery import collect_announcements
from ros2hermos._ioctl import _load_lib


rclpy.init(args=[])
node = rclpy.create_node('e3_external_query')
try:
    # A query may use the local fallback; bind that loaded wrapper to this run's
    # verified bundle without making a payload endpoint or acquiring a message.
    assert _load_lib() is not None
    bundle = Path(os.environ['RCLPY_HERMOS_KMOD']).resolve().parents[1]
    expected = hashlib.sha256((bundle / 'lib/libhermos_ioctl_wrapper.so').read_bytes()).digest()
    wrappers = {Path(line.split(maxsplit=5)[5]) for line in Path('/proc/self/maps').read_text().splitlines()
                if line.split(maxsplit=5)[-1].endswith('/libhermos_ioctl_wrapper.so')}
    assert wrappers and all(hashlib.sha256(path.read_bytes()).digest() == expected for path in wrappers)
    states = collect_announcements(node, timeout_sec=float(sys.argv[1]))
    print('E3_SNAPSHOTS ' + json.dumps([message_to_ordereddict(state) for state in states]), flush=True)
finally:
    node.destroy_node()
    rclpy.shutdown()
