"""Public Node Tensor facade used by the Python/C++ transport tests."""

import json
from contextlib import nullcontext
import os
import select
import signal
import sys

import torch
from rclpy.qos import QoSProfile, ReliabilityPolicy

from rclpy_agnocast import Node, SubscriptionLimits
from rclpy_agnocast_gpu_msgs.msg import GpuTensor


def trace(name):
    return torch.cuda.nvtx.range(name) if os.environ.get('G4_TRACE_REGION') == '1' else nullcontext()


def factory(runtime, arguments):
    role, topic, dtype = arguments
    node = Node('g4_tensor_' + role, context=runtime.ros_context)
    stream = torch.cuda.Stream()
    tensor_type = getattr(torch, dtype)
    shape = (64,) if dtype == 'uint8' else (4, 4)
    state = {'tensor': None, 'hold': False, 'ready': False}
    qos = QoSProfile(depth=4, reliability=ReliabilityPolicy.BEST_EFFORT)
    if role == 'publisher':
        endpoint = node.create_publisher(GpuTensor, topic, qos, transport='agnocast',
                                         max_message_bytes=4096)
    else:
        def callback(lease):
            with trace('g4_consume'):
                descriptor = lease.descriptor
                tensor = lease.as_tensor(stream=stream)
                assert tuple(tensor.shape) == shape and tensor.dtype == tensor_type
                with torch.cuda.stream(stream):
                    total = tensor.sum().item()
            if state['hold']:
                state['tensor'] = tensor[1:].detach()
            print('RECEIVED', json.dumps({'uid': descriptor['core']['uid'], 'sum': total,
                                         'shape': list(tensor.shape), 'dtype': str(tensor.dtype),
                                         'pid': os.getpid()}), flush=True)

        endpoint = node.create_subscription(
            GpuTensor, topic, callback, qos, transport='agnocast',
            limits=SubscriptionLimits(4, 16384, 4096))

    def command():
        nonlocal endpoint
        if not state['ready']:
            state['ready'] = True
            print('READY', role, os.getpid(), flush=True)
        if not select.select([sys.stdin], [], [], 0)[0]:
            return
        line = sys.stdin.readline().strip().split()
        if not line:
            os.kill(os.getpid(), signal.SIGTERM)
            return
        action = line[0]
        if action == 'publish':
            value = int(line[1])
            try:
                loan = endpoint.borrow_tensor(shape, tensor_type, 'cuda:0', stream)
            except RuntimeError as error:
                if 'full' not in str(error):
                    raise
                print('FULL', flush=True)
                return
            with loan, trace('g4_publish'):
                tensor = loan.as_tensor()
                assert tensor.data_ptr() == loan._handle.pointer
                with torch.cuda.stream(stream):
                    tensor.fill_(value)
                alias = tensor.detach()
                del tensor
                try:
                    endpoint.publish(loan)
                except RuntimeError as error:
                    assert 'aliases' in str(error)
                else:
                    raise AssertionError('published a live writable Tensor alias')
                del alias
                endpoint.publish(loan)
            print('PUB', value, flush=True)
        elif action == 'hold':
            state['hold'] = True
            print('HOLDING', flush=True)
        elif action == 'drop':
            state['tensor'] = None
            state['hold'] = False
            print('DROPPED', flush=True)
        elif action == 'close':
            try:
                node.destroy_subscription(endpoint)
            except RuntimeError:
                print('BUSY', flush=True)
            else:
                assert not runtime._native.closing
                print('CLOSED', flush=True)
        elif action == 'recreate':
            endpoint = node.create_subscription(
                GpuTensor, topic, callback, qos, transport='agnocast',
                limits=SubscriptionLimits(4, 16384, 4096))
            print('RECREATED', flush=True)
        elif action == 'stats':
            print('STATS', json.dumps(runtime.stats), flush=True)
        elif action == 'quit':
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            raise ValueError('unknown command: ' + action)

    node.create_timer(0.005, command)
    # Keep caller-owned streams alive until launcher runtime.shutdown completes.
    node.gpu_test_stream = stream
    return node
