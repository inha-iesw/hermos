"""Keep a real CUDA alias across an asynchronous callback and TERM drain."""

import json
import os
from threading import Thread
import time

import torch
from rclpy.qos import QoSProfile, ReliabilityPolicy
from rclpy.task import Future

from rclpy_hermos import Node, SubscriptionLimits
from rclpy_hermos_gpu_msgs.msg import GpuTensor


def factory(runtime, arguments):
    topic, = arguments
    node = Node('g4_async_reader', context=runtime.ros_context)
    stream = torch.cuda.Stream()
    gate = Future()
    state = {'ready': False, 'callbacks': 0}

    def complete_during_shutdown():
        # No executor callback is needed to complete this Future. The launcher
        # must resume the already-started coroutine after it stops new callbacks.
        deadline = time.monotonic() + 15
        while not runtime._closing:
            if time.monotonic() >= deadline:
                gate.set_exception(TimeoutError('TERM did not begin GPU shutdown'))
                return
            time.sleep(0.001)
        gate.set_result(None)

    async def callback(lease):
        state['callbacks'] += 1
        assert state['callbacks'] == 1
        tensor = lease.as_tensor(stream=stream)
        alias = tensor.detach()
        del tensor
        with torch.cuda.stream(stream):
            before = alias.sum().item()
        completer = Thread(target=complete_during_shutdown, daemon=True)
        completer.start()
        print('HOLDING', json.dumps({'pid': os.getpid(), 'sum': before,
                                     'active_owners': runtime.stats['active_owners']}), flush=True)
        await gate
        completer.join(timeout=1)
        assert not completer.is_alive()
        assert runtime._closing
        with torch.cuda.stream(stream):
            after = alias.sum().item()
        del alias
        assert after == before
        print('DONE', json.dumps({'sum': after, 'closing': runtime._closing,
                                 'callbacks': state['callbacks']}), flush=True)

    node.create_subscription(
        GpuTensor, topic, callback,
        QoSProfile(depth=4, reliability=ReliabilityPolicy.BEST_EFFORT),
        transport='hermos', limits=SubscriptionLimits(4, 16384, 4096))

    def ready():
        if not state['ready']:
            state['ready'] = True
            print('READY subscriber', os.getpid(), flush=True)

    node.create_timer(0.005, ready)
    # Runtime drain still polls stream events after the callback has returned.
    node.gpu_test_stream = stream
    return node
