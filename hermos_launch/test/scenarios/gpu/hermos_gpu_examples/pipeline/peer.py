"""Three known GPU payloads, with control files carrying no Tensor storage."""

import json
import os
from pathlib import Path
import signal
import time


VALUES = (3, 17, 91)


def write_proof(path, value):
    """Publish complete control/proof files atomically within the output directory."""
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def node_proof(node, topic):
    """Record the resolved ROS settings and the process namespace, not IPC handles."""
    mapped = set()
    for line in Path('/proc/self/maps').read_text().splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) == 6 and fields[5].startswith('/') and 'hermos' in fields[5]:
            name = Path(fields[5].removesuffix(' (deleted)')).name
            if name.endswith('.so') or '.so.' in name:
                mapped.add(fields[5])
    return dict(pid=os.getpid(), name=node.get_name(), namespace=node.get_namespace(),
                topic=node.resolve_topic_name(topic), label=node.get_parameter('label').value,
                ipc=os.readlink('/proc/self/ns/ipc'), mnt=os.readlink('/proc/self/ns/mnt'),
                factory_module=str(Path(__file__).resolve()), mapped_libraries=sorted(mapped))


def cpu_factory(context, arguments):
    """Create a real CPU endpoint; the first post-ACK timer announces app readiness."""
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from rclpy_hermos import Node
    from sensor_msgs.msg import Image

    directory = Path(arguments[0])
    node = Node('cpu_factory', context=context)
    node.declare_parameter('label', '')
    node.create_publisher(Image, 'image',
                          QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT),
                          transport='hermos', max_message_bytes=4096)

    def ready():
        write_proof(directory / 'cpu.json', node_proof(node, 'image'))
        timer.cancel()

    timer = node.create_timer(0.01, ready)
    return node


def tensor_factory(runtime, arguments):
    """Create a writer or reader with direct shared GPU storage and bounded work."""
    import torch
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from rclpy_hermos import Node, SubscriptionLimits
    from rclpy_hermos_gpu_msgs.msg import GpuTensor
    from std_msgs.msg import String

    role, output = arguments
    if role not in ('writer', 'reader'):
        raise ValueError('expected writer or reader')
    directory = Path(output)
    node = Node('tensor_factory', context=runtime.ros_context)
    node.declare_parameter('label', '')
    stream = torch.cuda.Stream()
    # The runtime drains after factory dispatch stops; its bound stream stays alive.
    node.pipeline_stream = stream
    qos = QoSProfile(depth=4, reliability=ReliabilityPolicy.BEST_EFFORT)
    state = dict(started=None, ready=False, published=0, acknowledged=0, received=[],
                 outside=None, finished=False)

    if role == 'writer':
        endpoint = node.create_publisher(GpuTensor, 'tensor', qos, transport='hermos',
                                         max_message_bytes=4096)
        dds = node.create_publisher(String, 'inside_status', 10)

        def outside(message):
            if message.data != 'outside DDS ready':
                raise ValueError('unexpected outside DDS payload')
            state['outside'] = message.data

        node.create_subscription(String, 'outside_status', outside, 10)
    else:
        def receive(lease):
            index = len(state['received'])
            if index >= len(VALUES):
                raise ValueError('received an unexpected extra Tensor')
            tensor = lease.as_tensor(stream=stream)
            if tuple(tensor.shape) != (64,) or tensor.dtype != torch.uint8:
                raise ValueError('unexpected Tensor shape or dtype')
            with torch.cuda.stream(stream):
                total = int(tensor.sum().item())
                matches = bool(torch.all(tensor == VALUES[index]).item())
            if not matches or total != 64 * VALUES[index]:
                raise ValueError('GPU payload content mismatch')
            del tensor
            state['received'].append(dict(value=VALUES[index], checksum=total))
            write_proof(directory / 'reader.json',
                        dict(**node_proof(node, 'tensor'), received=state['received']))
            # This acknowledges consumption; no GPU descriptor or payload crosses files.
            (directory / f'ack-{index}').touch(exist_ok=False)

        node.create_subscription(GpuTensor, 'tensor', receive, qos, transport='hermos',
                                 limits=SubscriptionLimits(4, 16384, 4096))

    def progress():
        if state['started'] is None:
            state['started'] = time.monotonic()
        if time.monotonic() - state['started'] > 30:
            raise RuntimeError('GPU pipeline did not finish within its application deadline')
        if not state['ready']:
            # Dispatch starts only after supervisor registration ACK. Factory return
            # alone must never be used as evidence that an application is ready.
            write_proof(directory / f'{role}-ready.json', node_proof(node, 'tensor'))
            state['ready'] = True
        if role == 'reader' or state['finished']:
            return
        dds.publish(String(data='inside DDS ready'))
        if not (directory / 'reader-ready.json').exists() or not (directory / 'cpu.json').exists():
            return
        index = state['acknowledged']
        if state['published'] > index:
            if not (directory / f'ack-{index}').exists():
                return
            state['acknowledged'] += 1
            index += 1
        if index == len(VALUES):
            if state['outside'] is None or not (directory / 'outside.json').exists():
                return
            write_proof(directory / 'writer.json',
                        dict(**node_proof(node, 'tensor'), published=list(VALUES),
                             acknowledged=index, outside=state['outside']))
            state['finished'] = True
            os.kill(os.getpid(), signal.SIGTERM)
            return
        with endpoint.borrow_tensor((64,), torch.uint8, 'cuda:0', stream) as loan:
            tensor = loan.as_tensor()
            with torch.cuda.stream(stream):
                tensor.fill_(VALUES[index])
            del tensor
            endpoint.publish(loan)
        state['published'] += 1

    node.create_timer(0.02, progress)
    return node
