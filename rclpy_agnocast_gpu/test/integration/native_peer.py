"""Registered CUDA ownership cases using a separate native source process."""

import copy
import gc
import json
import os
from pathlib import Path
import signal
import sys
import time

import torch

from rclpy_agnocast_gpu import GpuRuntime
from rclpy_agnocast_gpu import _native
from rclpy_agnocast_gpu.tensor.export import StreamExport, as_tensor


def until(check, runtime, timeout=3):
    deadline = time.monotonic() + timeout
    while True:
        runtime.poll()
        value = check()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError('GPU ownership did not converge')
        time.sleep(0.001)


def reject(call):
    try:
        call()
    except (RuntimeError, ValueError, BufferError):
        return
    raise AssertionError('invalid GPU operation was accepted')


def borrow(native, stream, shape=(64,), code=1, bits=8):
    return native.borrow(shape, code, bits, stream.cuda_stream, 1)


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value) + '\n')
    temporary.replace(path)


class RemoteSource:
    """Exchange only metadata/control files; payloads stay in CUDA VMM storage."""

    def __init__(self, directory, runtime):
        self.directory, self.runtime = Path(directory), runtime
        self.sequence = 0

    def request(self, action, **arguments):
        self.sequence += 1
        request = self.directory / f'request-{self.sequence}.json'
        response = self.directory / f'response-{self.sequence}.json'
        write_json(request, {'action': action, **arguments})
        until(response.exists, self.runtime, timeout=5)
        value = json.loads(response.read_text())
        assert value['pid'] != os.getpid(), 'CUDA IPC source must be a separate process'
        if 'descriptor' in value:
            assert value['descriptor']['core']['publisher_pid'] == value['pid']
        return value

    def publish(self, value=37, *, pending=False):
        return self.request('publish_pending' if pending else 'publish', value=value)['descriptor']


def aliases(runtime, writer, reader, source):
    native = runtime._native
    loan = borrow(native, writer)
    tensor = as_tensor(lambda: loan, device=0, stream=writer)
    assert tensor.data_ptr() == loan.pointer
    alias = tensor[1:].detach()
    with torch.cuda.stream(writer):
        tensor.fill_(37)
    del tensor
    reject(loan.prepare)
    assert loan.exports == 1
    del alias
    loan.prepare()  # Own-process publication is never imported back into its context.
    descriptor = source.publish()
    read = native.map(descriptor, reader.cuda_stream, 2)
    pointer = read.pointer
    tensor = as_tensor(lambda: read, device=0, stream=reader)
    assert tensor.data_ptr() == pointer
    alias = tensor[2:].detach()
    with torch.cuda.stream(reader):
        assert tensor.sum().item() == 64 * 37
        assert alias.sum().item() == 62 * 37  # Warm this shape/alignment variant.
    gate = _native._TestGate(reader.cuda_stream)
    try:
        with torch.cuda.stream(reader):
            total = alias.sum()
        del tensor, read
        assert native.owner_active(2) == 1
        del alias
        assert native.owner_active(2) == 1  # GPU work outlives the last Python alias.
        assert native.try_close() is False
        assert native.closing
    finally:
        gate.release()
    until(lambda: native.owner_active(2) == 0, runtime)
    assert total.item() == 62 * 37


def cancel(runtime, writer, reader, source):
    del reader, source
    native = runtime._native
    loan = borrow(native, writer)
    tensor = as_tensor(lambda: loan, device=0, stream=writer)
    with torch.cuda.stream(writer):
        tensor.fill_(1)
    writer.synchronize()  # Warm the actual kernel before installing the gate.
    gate = _native._TestGate(writer.cuda_stream)
    try:
        with torch.cuda.stream(writer):
            tensor.fill_(2)
        alias = tensor.detach()
        loan.cancel()
        del tensor
        assert loan.exports == 1
        del alias, loan
        assert native.owner_active(1) == 1
        try:
            borrow(native, writer)
        except RuntimeError as error:
            assert 'full' in str(error)
        else:
            raise AssertionError('pending unpublished write allowed block reuse')
    finally:
        gate.release()
    until(lambda: native.owner_active(1) == 0, runtime)

    def replacement():
        try:
            return borrow(native, writer)
        except RuntimeError as error:
            if 'full' not in str(error):
                raise
            return None
    until(replacement, runtime).cancel()


def producer_ready(runtime, writer, reader, source):
    del writer
    native = runtime._native
    # Finish reader allocator/module and event initialization before the remote
    # ready wait. No synchronizing warm-up occurs while the producer is gated.
    with torch.cuda.stream(reader):
        warm = torch.ones(64, device='cuda', dtype=torch.uint8)
        assert warm.sum().item() == 64
        done = torch.cuda.Event()
        done.record()
    done.synchronize()
    del warm
    descriptor = source.publish(19, pending=True)
    read = native.map(descriptor, reader.cuda_stream, 2)
    tensor = as_tensor(lambda: read, device=0, stream=reader)
    with torch.cuda.stream(reader):
        total = tensor.sum()
        done.record()
    assert not done.query()
    del tensor, read
    assert native.owner_active(2) == 1
    assert source.request('release')['released']
    done.synchronize()
    assert total.item() == 64 * 19
    until(lambda: native.owner_active(2) == 0, runtime)


def capsule(runtime, writer, reader, source):
    native = runtime._native
    loan = borrow(native, writer)
    capsule = loan.dlpack(writer.cuda_stream)
    assert loan.exports == 1
    _native.discard_dlpack(capsule)
    _native.discard_dlpack(capsule)
    assert loan.exports == 0
    del capsule
    loan.prepare()
    descriptor = source.publish()
    read = native.map(descriptor, reader.cuda_stream, 2)
    capsule = read.dlpack(reader.cuda_stream)
    with torch.cuda.stream(reader):
        tensor = torch.utils.dlpack.from_dlpack(capsule)
    reject(lambda: torch.utils.dlpack.from_dlpack(capsule))
    _native.discard_dlpack(capsule)
    del capsule, read
    assert native.owner_active(2) == 1
    del tensor
    until(lambda: native.owner_active(2) == 0, runtime)


def layout(runtime, writer, reader, source):
    native = runtime._native
    for shape, code, bits in (((-1,), 1, 8), ((2**62, 4), 1, 8), ((65,), 2, 32),
                              ((1,), 2, 8), ((0, 2**62, 4), 1, 8)):
        reject(lambda: borrow(native, writer, shape, code, bits))
    descriptor = source.publish()
    for mutate in (lambda d: d.update(byte_offset=1),
                   lambda d: d.update(strides=[4]),
                   lambda d: d['core'].update(device_id=1),
                   lambda d: d.update(allocation_bytes=255)):
        invalid = copy.deepcopy(descriptor)
        mutate(invalid)
        reject(lambda: native.map(invalid, reader.cuda_stream, 2))
    assert native.owner_active(2) == 0
    read = native.map(descriptor, reader.cuda_stream, 2)
    tensor = as_tensor(lambda: read, device=0, stream=reader)
    with torch.cuda.stream(reader):
        assert tensor.sum().item() == 64 * 37
    del tensor, read


def generations(runtime, writer, reader, source):
    del writer
    native = runtime._native
    first = source.publish(11)
    read1 = native.map(first, reader.cuda_stream, 2)
    read2 = native.map(first, reader.cuda_stream, 3)
    tensor1 = as_tensor(lambda: read1, device=0, stream=reader)
    tensor2 = as_tensor(lambda: read2, device=0, stream=reader)
    alias = tensor2.detach()
    del read1, read2, tensor1, tensor2
    # The source waits beyond the publication grace before probing. FULL must
    # therefore be explained by the retained read, not merely the grace timer.
    assert source.request('probe')['full']
    with torch.cuda.stream(reader):
        assert alias.sum().item() == 64 * 11
    del alias
    until(lambda: native.owner_active(2) == native.owner_active(3) == 0, runtime)
    second = source.publish(23)
    assert first['core']['uid'] != second['core']['uid']
    reject(lambda: native.map(first, reader.cuda_stream, 2))
    read = native.map(second, reader.cuda_stream, 2)
    tensor = as_tensor(lambda: read, device=0, stream=reader)
    with torch.cuda.stream(reader):
        assert tensor.sum().item() == 64 * 23
    del tensor, read


def joined_streams(runtime, writer, reader, source):
    del writer
    native = runtime._native
    descriptor = source.publish()
    read = native.map(descriptor, reader.cuda_stream, 2)
    tensor = as_tensor(lambda: read, device=0, stream=reader)
    other = torch.cuda.Stream()
    # First propagate producer readiness to the additional consumer stream.
    other.wait_stream(reader)
    with torch.cuda.stream(other):
        assert tensor.sum().item() == 64 * 37  # Warm the actual reduction.
    gate = _native._TestGate(other.cuda_stream)
    try:
        with torch.cuda.stream(other):
            total = tensor.sum()
        # This explicit completion dependency, not record_stream(), returns all
        # declared use to the one stream bound to the native ReadHandle.
        reader.wait_stream(other)
        del tensor, read
        assert native.owner_active(2) == 1
        assert source.request('probe')['full']
        assert native.owner_active(2) == 1
    finally:
        gate.release()
    until(lambda: native.owner_active(2) == 0, runtime)
    assert total.item() == 64 * 37


def stream_mismatch(runtime, writer, reader, source):
    native = runtime._native
    loan = borrow(native, writer)
    reject(lambda: loan.dlpack(reader.cuda_stream))
    assert loan.exports == 0 and loan.state == 'BORROWED'
    # A different current stream is restored after the facade deliberately
    # consumes on its bound stream; the failed native export did not consume it.
    with torch.cuda.stream(reader):
        tensor = as_tensor(lambda: loan, device=0, stream=writer)
        assert torch.cuda.current_stream() == reader
    assert tensor.data_ptr() == loan.pointer and loan.exports == 1
    with torch.cuda.stream(writer):
        tensor.fill_(13)
        assert tensor.sum().item() == 64 * 13
    del tensor
    loan.prepare()

    descriptor = source.publish()
    mapped = []

    def map_after_stream_check():
        read = native.map(descriptor, reader.cuda_stream, 2)
        mapped.append(read.pointer)
        # Exercise an error after real mapping but before native ownership is
        # transferred to DLPack. Correct-stream export must remain available.
        reject(lambda: read.dlpack(writer.cuda_stream))
        assert read.pointer == mapped[-1] and native.owner_active(2) == 1
        return read

    exporter = StreamExport(map_after_stream_check, device=0, stream=reader.cuda_stream)
    with torch.cuda.stream(writer):
        reject(lambda: torch.utils.dlpack.from_dlpack(exporter, copy=False))
    assert not mapped and native.owner_active(2) == 0
    with torch.cuda.stream(reader):
        tensor = torch.utils.dlpack.from_dlpack(exporter, copy=False)
        total = tensor.sum()
    assert len(mapped) == 1 and tensor.data_ptr() == mapped[0]
    del tensor, exporter
    until(lambda: native.owner_active(2) == 0, runtime)
    assert total.item() == 64 * 37


CASES = {'aliases': aliases, 'cancel': cancel, 'producer_ready': producer_ready,
         'capsule': capsule, 'layout': layout, 'generations': generations,
         'joined_streams': joined_streams, 'stream_mismatch': stream_mismatch}


def main():
    case, output, control = sys.argv[1:]
    # The source may exit after writing its final proof. A simultaneous TERM
    # from the supervisor must not interrupt this reader's final proof read.
    stopped = []
    signal.signal(signal.SIGTERM, lambda signum, frame: stopped.append(signum))
    torch.cuda.init()
    runtime = GpuRuntime(block_count=1, block_bytes=256, device=0,
                         max_reads=2, max_message_bytes=256).initialise()
    runtime.register()
    writer, reader = torch.cuda.Stream(), torch.cuda.Stream()
    source = None if case == 'cancel' else RemoteSource(control, runtime)
    CASES[case](runtime, writer, reader, source)
    gc.collect()
    runtime.shutdown()
    assert not stopped, 'source exited before reader drain/finish handshake'
    proof = {'case': case, 'passed': True, 'pid': os.getpid(), 'stats': runtime.stats}
    if source is not None:
        proof['source'] = source.request('finish')
        assert proof['source']['finished']
    write_json(Path(output), proof)


if __name__ == '__main__':
    main()
