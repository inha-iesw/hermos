"""Controlled installed-API CPU peer for metadata and native-counter acceptance."""

import json
import os
from pathlib import Path


def write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value) + '\n')
    temporary.replace(path)


def create(context, arguments):
    """Create endpoints before ACK; execute test commands only on the executor."""
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from rclpy_hermos import Node, SubscriptionLimits
    from sensor_msgs.msg import Image

    role, output, topic = arguments
    directory = Path(output)
    node = Node('e3_' + role, context=context)
    qos = QoSProfile(depth=2, reliability=ReliabilityPolicy.BEST_EFFORT)
    received = []
    retained = []
    retain = False
    publisher = subscription = None
    if role == 'writer':
        publisher = node.create_publisher(
            Image, topic, qos, transport='hermos', max_message_bytes=4096)
    elif role == 'reader':
        def receive(message):
            with message.data.buffer() as data:
                assert bytes(data) == bytes([message.header.stamp.sec]) * 16
            if retain:
                retained.append(message.data.buffer())
            received.append(message.header.stamp.sec)
            write(directory / 'received.json', received)

        subscription = node.create_subscription(
            Image, topic, receive, qos, transport='hermos',
            limits=SubscriptionLimits(16, 65536, 4096))
    else:
        raise ValueError('expected writer or reader')

    previous = None
    published = 0
    ready = False

    def progress():
        nonlocal previous, published, publisher, subscription, ready, retain
        if not ready:
            write(directory / (role + '-ready.json'), {
                'pid': os.getpid(), 'session': os.environ['HERMOS_SESSION_ID'],
                'ipc': os.stat('/proc/self/ns/ipc').st_ino})
            ready = True
        command = directory / (role + '-command.json')
        if not command.exists():
            return
        request = json.loads(command.read_text())
        if request['id'] == previous:
            return
        action = request['action']
        if action == 'publish' and publisher is not None:
            for _ in range(request['count']):
                published += 1
                with publisher.borrow_message() as message:
                    message.data.resize(16)
                    message.header.stamp.sec = published
                    message.height, message.width, message.step = 1, 16, 16
                    with message.data.buffer() as data:
                        data[:] = bytes([published]) * 16
                    publisher.publish(message)
            result = {'published': published}
        elif action == 'close' and publisher is not None:
            node.destroy_publisher(publisher)
            publisher = None
            result = {'closed': True}
        elif action == 'close' and subscription is not None:
            node.destroy_subscription(subscription)
            subscription = None
            result = {'closed': True}
        elif action == 'retain' and subscription is not None:
            retain = True
            result = {'retaining': True}
        elif action == 'release' and subscription is not None:
            contents = [list(view) for view in retained]
            for view in retained:
                view.release()
            retained.clear()
            retain = False
            result = {'contents': contents}
        elif action == 'snapshot':
            from ros2hermos._ioctl import self_ns_snapshot
            from rosidl_runtime_py.convert import message_to_ordereddict
            result = message_to_ordereddict(self_ns_snapshot(domain_id=context.get_domain_id()))
        else:
            raise ValueError('invalid observation test command')
        previous = request['id']
        write(directory / (role + '-reply.json'), dict(id=previous, result=result))

    node.create_timer(0.01, progress)
    return node
