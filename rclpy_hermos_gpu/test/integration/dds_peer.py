"""DDS topic and parameter-service oracle for private GPU epoch coexistence."""

import json
import os
from pathlib import Path
import signal
import sys


def _node(context, role, topic, result_path):
    from rclpy.node import Node
    from rclpy.utilities import get_rmw_implementation_identifier
    from rcl_interfaces.msg import ParameterType
    from rcl_interfaces.srv import GetParameters
    from std_msgs.msg import String

    if role not in ('a', 'b'):
        raise ValueError('DDS role must be a or b')
    other = 'b' if role == 'a' else 'a'
    prefix = topic.removeprefix('/')
    node = Node(f'{prefix}_{role}', context=context)
    own_payload, expected = f'{topic}:{role}:payload', f'{topic}:{other}:payload'
    own_parameter, expected_parameter = f'{topic}:{role}:parameter', f'{topic}:{other}:parameter'
    node.declare_parameter('proof', own_parameter)
    publisher = node.create_publisher(String, f'{topic}/{role}', 10)
    client = node.create_client(GetParameters, f'/{prefix}_{other}/get_parameters')
    state = {'received': None, 'parameter': None, 'future': None, 'written': False}

    def receive(message):
        if message.data != expected:
            raise AssertionError(f'unexpected DDS payload: {message.data!r}')
        state['received'] = message.data

    node.create_subscription(String, f'{topic}/{other}', receive, 10)

    def progress():
        publisher.publish(String(data=own_payload))
        future = state['future']
        if state['parameter'] is None and future is None and client.service_is_ready():
            request = GetParameters.Request()
            request.names = ['proof']
            state['future'] = client.call_async(request)
        elif future is not None and future.done():
            response = future.result()
            if (len(response.values) != 1 or
                    response.values[0].type != ParameterType.PARAMETER_STRING or
                    response.values[0].string_value != expected_parameter):
                raise AssertionError(f'unexpected DDS parameter response: {response!r}')
            state['parameter'] = response.values[0].string_value
            state['future'] = None
        if state['received'] is None or state['parameter'] is None or state['written']:
            return
        destination = Path(result_path)
        temporary = destination.with_suffix('.tmp')
        temporary.write_text(json.dumps({
            'pid': os.getpid(), 'role': role,
            'rmw': get_rmw_implementation_identifier(),
            'received': state['received'], 'parameter': state['parameter'],
            'ipc': os.readlink('/proc/self/ns/ipc'),
            'mnt': os.readlink('/proc/self/ns/mnt'),
            'shm_device': os.stat('/dev/shm').st_dev,
        }, indent=2) + '\n')
        temporary.replace(destination)
        state['written'] = True

    node.create_timer(0.05, progress)
    return node


def factory(runtime, arguments):
    """Create a DDS-only Node in the launcher's explicitly registered GPU process."""
    role, topic, result_path = arguments
    return _node(runtime.ros_context, role, topic, result_path)


def outside(arguments):
    """Run the stock DDS participant without importing or initializing GPU/Hermos."""
    import rclpy
    from rclpy.context import Context
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.signals import SignalHandlerOptions

    role, topic, result_path = arguments
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        del signum, frame
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    context = Context()
    node = executor = None
    try:
        rclpy.init(args=[], context=context, signal_handler_options=SignalHandlerOptions.NO)
        node = _node(context, role, topic, result_path)
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(node)
        while not stopping and context.ok():
            executor.spin_once(timeout_sec=0.02)
    finally:
        if executor is not None:
            executor.shutdown(timeout_sec=0)
        if node is not None:
            node.destroy_node()
        if context.ok():
            context.shutdown()
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 5 or sys.argv[1] != '--outside':
        raise SystemExit('usage: dds_peer.py --outside ROLE TOPIC RESULT')
    raise SystemExit(outside(sys.argv[2:]))
