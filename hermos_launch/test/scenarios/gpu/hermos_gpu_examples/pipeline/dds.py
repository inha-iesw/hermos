"""An ordinary DDS Node outside the supervised GPU namespace."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time

from .peer import node_proof, write_proof


def has_expected_gpu_metadata(snapshot, *, writer_pid, reader_pid, domain_id, ipc_ns_inode):
    """Recognize three native descriptor transfers without claiming GPU completion."""
    if (snapshot.get('schema_version') != 2 or snapshot.get('domain_id') != domain_id or
            snapshot.get('ipc_ns_inode') != ipc_ns_inode or not snapshot.get('session_id')):
        return False
    topic = next((item for item in snapshot['topics'] if item['topic_name'] == '/camera/frames'), None)
    if (topic is None or topic['domain_id'] != domain_id or
            topic['type_name'] != 'rclpy_hermos_gpu_msgs/msg/GpuTensor' or
            len(topic['publishers']) != 1 or len(topic['subscribers']) != 1):
        return False
    publisher, subscriber = topic['publishers'][0], topic['subscribers'][0]
    return (publisher['pid'] == writer_pid and subscriber['pid'] == reader_pid and
            not publisher['is_bridge'] and not subscriber['is_bridge'] and
            publisher['statistics_status'] == subscriber['statistics_status'] == 'available' and
            publisher['supported_fields'] & 1 != 0 and subscriber['supported_fields'] & 2 != 0 and
            publisher['published_total'] == subscriber['received_total'] == 3)


def observer_proof(directory):
    """Capture the direct observer while alive; no GPU IPC handles are read."""
    paths = list(directory.glob('hermos-group-*/supervisor/epoch-0/ready.json'))
    if len(paths) != 1:
        raise RuntimeError('expected one registered pipeline group')
    ready = json.loads(paths[0].read_text())
    epoch_dir = Path(ready['epoch_dir'])
    recovery = json.loads((epoch_dir.parent / 'recovery.json').read_text())['epochs'][-1]
    peer = next(peer for peer in recovery['peers'] if peer['id'] == 'observer')
    process = Path('/proc') / str(peer['pid'])
    libraries = {line.split(maxsplit=5)[5] for line in (process / 'maps').read_text().splitlines()
                 if len(line.split(maxsplit=5)) == 6 and line.split(maxsplit=5)[5].startswith('/')}
    relevant = sorted(path for path in libraries if 'hermos' in path and
                      (Path(path).name.endswith('.so') or '.so.' in Path(path).name))
    environment = dict(value.split('=', 1) for value in (process / 'environ').read_bytes().decode().split('\0')
                       if '=' in value)
    wrappers = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in relevant
                if Path(path).name == 'libhermos_ioctl_wrapper.so'}
    return dict(pid=peer['pid'], ipc=os.readlink(process / 'ns/ipc'),
                mnt=os.readlink(process / 'ns/mnt'), preload=environment.get('LD_PRELOAD', ''),
                domain_id=int(environment.get('ROS_DOMAIN_ID') or '0'),
                session_id=recovery['session_id'],
                manifest=json.loads(Path(peer['manifest']).read_text()),
                mapped_libraries=relevant, mapped_names=sorted({Path(path).name for path in libraries}),
                wrappers=wrappers,
                argv=[value for value in (process / 'cmdline').read_bytes().decode().split('\0') if value])


def main():
    """Exchange DDS status until the finite GPU writer records successful completion."""
    import rclpy
    from rclpy.context import Context
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.node import Node
    from rclpy.signals import SignalHandlerOptions
    from rclpy.utilities import remove_ros_args
    from ros2hermos.discovery import GOSSIP_TOPIC, gossip_qos
    from ros2hermos_discovery_msgs.msg import HermosDaemonState
    from rosidl_runtime_py.convert import message_to_ordereddict
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
    observed = False

    def metadata(message):
        nonlocal observed
        if observed:
            return
        directory = arguments.output_directory
        paths = [directory / (role + '-ready.json') for role in ('writer', 'reader')]
        if not all(path.exists() for path in paths):
            return
        writer, reader = [json.loads(path.read_text()) for path in paths]
        snapshot = message_to_ordereddict(message)
        if not has_expected_gpu_metadata(snapshot, writer_pid=writer['pid'], reader_pid=reader['pid'],
                domain_id=context.get_domain_id(), ipc_ns_inode=int(writer['ipc'].split('[')[1][:-1])):
            return
        write_proof(directory / 'observer.json', observer_proof(directory))
        write_proof(directory / 'metadata.json', snapshot)
        observed = True

    def receive(message):
        if message.data != 'inside DDS ready':
            raise ValueError('unexpected inside DDS payload')
        if not observed:
            return
        write_proof(arguments.output_directory / 'outside.json',
                    dict(**node_proof(node, 'inside_status'), received=message.data,
                         preload=os.environ.get('LD_PRELOAD', '')))

    node.create_subscription(String, 'inside_status', receive, 10)
    node.create_subscription(HermosDaemonState, GOSSIP_TOPIC, metadata, gossip_qos())
    # The writer needs this DDS acknowledgement only after its three payloads.
    # Holding it until metadata arrives keeps all real endpoints alive to inspect.
    node.create_timer(0.02, lambda: publisher.publish(String(data='outside DDS ready')) if observed else None)
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
