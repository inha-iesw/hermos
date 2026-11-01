"""Separate registered producer for native CUDA IPC ownership tests."""

import json
import os
from pathlib import Path
import signal
import sys
import time

import torch

from rclpy_agnocast_gpu import GpuRuntime, _native
from rclpy_agnocast_gpu.tensor.export import as_tensor
from native_peer import borrow, write_json


def main():
    directory = Path(sys.argv[1])
    stopped = False

    def stop(signum, frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    torch.cuda.init()
    runtime = GpuRuntime(block_count=1, block_bytes=256, device=0,
                         max_reads=2, max_message_bytes=256).initialise()
    runtime.register()
    stream = torch.cuda.Stream()
    sequence = 1
    gate = None
    published_at = 0.0

    def reserve():
        deadline = time.monotonic() + 3
        while not stopped:
            runtime.poll()
            try:
                return borrow(runtime._native, stream)
            except RuntimeError as error:
                if 'full' not in str(error):
                    raise
            if time.monotonic() >= deadline:
                raise AssertionError('remote source pool did not become reusable')
            time.sleep(0.001)
        raise RuntimeError('remote source stopped while waiting for a block')

    try:
        while not stopped:
            runtime.poll()
            request = directory / f'request-{sequence}.json'
            if not request.exists():
                time.sleep(0.001)
                continue
            command = json.loads(request.read_text())
            action = command['action']
            response = {'pid': os.getpid()}
            if action in ('publish', 'publish_pending'):
                loan = reserve()
                tensor = as_tensor(lambda: loan, device=0, stream=stream)
                assert tensor.data_ptr() == loan.pointer
                if action == 'publish_pending':
                    with torch.cuda.stream(stream):
                        tensor.fill_(0)
                    stream.synchronize()  # Warm the exact producer kernel first.
                    gate = _native._TestGate(stream.cuda_stream)
                with torch.cuda.stream(stream):
                    tensor.fill_(command['value'])
                del tensor
                response['descriptor'] = loan.prepare()
                published_at = time.monotonic()
            elif action == 'release':
                assert gate is not None
                gate.release()
                gate = None
                response['released'] = True
            elif action == 'probe':
                while time.monotonic() < published_at + 0.15 and not stopped:
                    runtime.poll()
                    time.sleep(0.001)
                try:
                    loan = borrow(runtime._native, stream)
                except RuntimeError as error:
                    if 'full' not in str(error):
                        raise
                    response['full'] = True
                else:
                    loan.cancel()
                    response['full'] = False
            elif action == 'finish':
                runtime.shutdown()
                response.update(finished=True, stats=runtime.stats)
                write_json(directory / 'source-proof.json', response)
                write_json(directory / f'response-{sequence}.json', response)
                return
            else:
                raise AssertionError('unexpected native source command: ' + action)
            write_json(directory / f'response-{sequence}.json', response)
            sequence += 1
    finally:
        if gate is not None:
            gate.release()
            gate = None
        runtime.shutdown()


if __name__ == '__main__':
    main()
