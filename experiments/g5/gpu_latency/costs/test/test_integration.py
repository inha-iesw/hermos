"""Actual Python endpoint delegation and restoration, without initializing CUDA."""

from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest

from experiments.g5.gpu_latency import dds_endpoint, peer
from experiments.g5.gpu_latency.costs.peer import Recorder, install
from rclpy_hermos_gpu.tensor.loan import TensorLoan


@pytest.mark.parametrize('failure', [None, 'prepare', 'conversion', 'publish'])
def test_cost_patches_preserve_prepare_commit_and_endpoint_identity(monkeypatch, failure):
    descriptor = dict(core=dict(vmm_socket_path='/tmp/cost-unit.sock',
                                event_handle=list(range(64)), publisher_pid=1, device_id=0,
                                block_id=0, uid=3, byte_size=4),
                      dtype_code=1, dtype_bits=8, dtype_lanes=1, shape=[4], strides=[1],
                      byte_offset=0, allocation_bytes=4)
    calls = []

    def prepare():
        calls.append('prepare')
        if failure == 'prepare':
            raise RuntimeError('diagnostic test failure')
        return descriptor

    def publish(message):
        assert message.core.uid == 3
        calls.append('publish')
        if failure == 'publish':
            raise RuntimeError('diagnostic test failure')

    if failure == 'conversion':
        def fail_conversion(_):
            raise RuntimeError('diagnostic test failure')
        monkeypatch.setattr(dds_endpoint, 'to_ros_message', fail_conversion)
    native_loan = SimpleNamespace(prepare=prepare, cancel=lambda: calls.append('cancel'))
    native = SimpleNamespace(borrow=lambda: native_loan)
    runtime = SimpleNamespace(_native=native, _check_admission=lambda: None)
    endpoint = object.__new__(dds_endpoint.DDSPublisher)
    endpoint._gpu_runtime = runtime
    endpoint._closing = False
    real_publisher = endpoint._publisher = SimpleNamespace(publish=publish)
    original_publish = dds_endpoint.DDSPublisher.publish
    recorder = Recorder()
    with install(runtime, recorder):
        recorder.begin()
        loan = TensorLoan(endpoint, runtime._native.borrow(), object())
        if failure:
            with pytest.raises(RuntimeError, match='diagnostic test failure'):
                with loan:
                    endpoint.publish(loan)
        else:
            with loan:
                endpoint.publish(loan)
        recorder.end(0)
        assert endpoint._publisher is real_publisher
        assert ('cancel' in calls) is (failure == 'prepare')
        assert loan._terminal is True
        stages = [event['stage'] for event in recorder.samples[0]['events']]
        assert stages[0] == 'prepare'
        if failure != 'prepare':
            assert 'descriptor_construct' in stages
        if failure not in ('prepare', 'conversion'):
            assert stages[-1] == 'cpu_publish'
    assert runtime._native is native
    assert dds_endpoint.DDSPublisher.publish is original_publish


@pytest.mark.parametrize('fail_buffer', [False, True])
def test_real_message_keeps_owner_identity_and_releases_exports_before_cleanup(fail_buffer):
    from rclpy_hermos.types.publisher import TypedPublisher
    from rclpy_hermos_gpu.endpoint import publisher

    class Owner:
        publisher_id = 41
        generation = exports = 0
        cancelled = published = False

        @property
        def snapshot(self):
            return {'generation': self.generation}

        def info(self, path, generation):
            assert not path or generation == self.generation
            return (dict(kind='message', members=('path',)) if not path else
                    dict(kind='string', generation=self.generation))

        def resize(self, path, generation, size):
            assert generation == self.generation and self.exports == 0
            self.storage = bytearray(size)
            self.generation += 1

        @contextmanager
        def buffer(self, path, generation):
            assert generation == self.generation
            self.exports += 1
            view = memoryview(self.storage)
            if fail_buffer:
                view = view.toreadonly()
            try:
                yield view
            finally:
                view.release()
                self.exports -= 1

        def publish(self, publisher_id):
            assert publisher_id == self.publisher_id and self.exports == 0
            self.published = True

        def cancel(self, publisher_id, quiet):
            assert publisher_id == self.publisher_id and quiet and self.exports == 0
            self.cancelled = True

    owner = Owner()
    typed = TypedPublisher(SimpleNamespace(id=41, borrow_message=lambda: owner), object())
    runtime, recorder = SimpleNamespace(_native=object()), Recorder()

    def publish_message():
        with typed.borrow_message() as message:
            assert message._owner is owner
            publisher.fill_descriptor(message, {'path': 'cuda-socket'})
            assert message._owner is owner and owner.exports == 0
            typed.publish(message)

    with install(runtime, recorder):
        recorder.begin()
        if fail_buffer:
            with pytest.raises(TypeError, match='read-only'):
                publish_message()
        else:
            publish_message()
        recorder.end(0)
    assert owner.cancelled and owner.exports == 0
    assert owner.published is not fail_buffer
    if not fail_buffer:
        assert owner.storage == b'cuda-socket'
    stages = [event['stage'] for event in recorder.samples[0]['events']]
    assert stages[:3] == ['descriptor_borrow', 'descriptor_resize', 'descriptor_fill_total']
    assert ('cpu_publish' in stages) is not fail_buffer


def test_tensor_export_total_wraps_the_existing_decode_map_and_dlpack_path(monkeypatch):
    from rclpy_hermos_gpu.tensor import lease

    descriptor, owner = {'descriptor': 1}, object()
    native = SimpleNamespace(map=lambda value, stream, endpoint: owner)
    runtime = SimpleNamespace(_native=native, _check_admission=lambda: None, device=0)
    monkeypatch.setattr(lease, 'read_descriptor', lambda _: descriptor)
    monkeypatch.setattr(lease, 'validate_torch_stream', lambda *args: 123)
    monkeypatch.setattr(lease, 'as_tensor', lambda acquire, **kwargs: acquire())
    tensor_lease = lease.TensorLease(object(), runtime, 7)
    recorder = Recorder()
    with install(runtime, recorder):
        recorder.begin()
        assert tensor_lease.as_tensor(stream=object()) is owner
        recorder.end(0)
    assert [event['stage'] for event in recorder.samples[0]['events']] == [
        'descriptor_decode', 'map', 'tensor_export_total']


@pytest.mark.parametrize('diagnostic', [False, True])
def test_peer_observer_times_the_existing_completion_wait_without_extra_syncs_or_scalar_reads(
        tmp_path, monkeypatch, diagnostic):
    config_path = tmp_path / 'config.json'
    config_path.write_text('{"descriptor_transport":"dds","size":1024,"samples":1,'
                           '"warmup":0,"period_ns":20000000,"depth":4,"topic":"/test"}')
    monkeypatch.setenv('G3_RECOVERY_MANIFEST', str(tmp_path / 'manifest'))
    monkeypatch.setenv('G3_RECOVERY_ACK', str(tmp_path / 'ack'))
    monkeypatch.setattr(peer, 'require_uninstrumented', lambda _: None)
    monkeypatch.setattr(peer, 'observe_sample_boundary', lambda *args, **kwargs: [])
    monkeypatch.setattr(peer, 'warmup_snapshot_due', lambda *args: False)
    calls, callbacks = [], []
    stream = SimpleNamespace(cuda_stream=123)
    completion = SimpleNamespace(record=lambda _: calls.append('record'),
                                 synchronize=lambda: calls.append('synchronize'))
    monkeypatch.setattr(peer.torch.cuda, 'Stream', lambda **kwargs: stream)
    monkeypatch.setattr(peer.torch.cuda, 'Event', lambda **kwargs: completion)
    monkeypatch.setattr(peer.torch.cuda, 'stream', lambda _: nullcontext())

    class Node:
        def __init__(self, *args, **kwargs):
            pass

        def create_timer(self, *args):
            pass

    import rclpy.node
    monkeypatch.setattr(rclpy.node, 'Node', Node)
    monkeypatch.setattr(dds_endpoint, 'DDSSubscription',
                        lambda node, topic, callback, qos, runtime: callbacks.append(callback))

    class Tensor:
        shape = (1024,)
        dtype = peer.torch.uint8

        def sum(self, **kwargs):
            return SimpleNamespace(item=lambda: calls.append('checksum-item') or 0)

        def __getitem__(self, key):
            return SimpleNamespace(view=lambda dtype: SimpleNamespace(
                item=lambda: calls.append('sequence-item') or 0))

    runtime = SimpleNamespace(device=0, ros_context=None, poll=lambda: None, stats={})
    recorder = Recorder() if diagnostic else None
    peer.factory(runtime, ['subscriber', str(config_path), str(tmp_path / 'subscriber.json')],
                 observer=recorder)
    callbacks[0](SimpleNamespace(as_tensor=lambda **kwargs: Tensor()))
    assert calls == ['record', 'synchronize', 'sequence-item', 'checksum-item']
    if recorder:
        assert recorder.samples[0]['sequence'] == 0
        assert [event['stage'] for event in recorder.samples[0]['events']] == ['gpu_completion_wait']
