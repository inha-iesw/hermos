"""Opt-in GPU stage clocks around existing calls; no extra CUDA synchronization."""

from contextlib import ExitStack
import json
from pathlib import Path
from types import SimpleNamespace
import time
from unittest.mock import patch


class Recorder:
    """Retain timestamps and status only, never operation arguments or returned owners."""

    def __init__(self, *, clock=time.monotonic_ns):
        self.clock = clock
        self.samples = []
        self._events = None

    def begin(self):
        if self._events is not None:
            raise RuntimeError('cost sample is already active')
        self._events = []

    def call(self, stage, operation, *args, **kwargs):
        if self._events is None:
            return operation(*args, **kwargs)
        started = self.clock()
        failed = False
        try:
            return operation(*args, **kwargs)
        except BaseException:
            failed = True
            raise
        finally:
            self._events.append(dict(stage=stage, started_ns=started,
                                     finished_ns=self.clock(), failed=failed))

    def end(self, sequence):
        if self._events is None:
            raise RuntimeError('cost sample is not active')
        self.samples.append(dict(sequence=sequence, events=self._events))
        self._events = None

    def save(self, path, config, role):
        if self._events is not None:
            raise RuntimeError('cannot save an unfinished cost sample')
        value = dict(version=1, timing_diagnostics=True, headline_eligible=False,
                     config=config, role=role, samples=self.samples)
        with path.with_name(path.stem + '-costs.json').open('x') as output:
            json.dump(value, output, indent=2)


class _Proxy:
    def __init__(self, target, recorder):
        self._target, self._recorder = target, recorder

    def __getattr__(self, name):
        return getattr(self._target, name)


class _NativeLoanProxy(_Proxy):
    def prepare(self):
        return self._recorder.call('prepare', self._target.prepare)


class NativeRuntimeProxy(_Proxy):
    """Preserve native ownership: only borrowed handles gain a forwarding prepare call."""

    def borrow(self, *args, **kwargs):
        return _NativeLoanProxy(self._target.borrow(*args, **kwargs), self._recorder)

    def map(self, *args, **kwargs):
        return self._recorder.call('map', self._target.map, *args, **kwargs)


class _MetadataOwnerProxy(_Proxy):
    def resize(self, *args, **kwargs):
        return self._recorder.call('descriptor_resize', self._target.resize, *args, **kwargs)


class _PublisherProxy(_Proxy):
    def publish(self, *args, **kwargs):
        return self._recorder.call('cpu_publish', self._target.publish, *args, **kwargs)


def fill_with_costs(recorder, fill, message, descriptor):
    """Pass a scoped owner view; never replace the real message's owner or cleanup."""
    observed = SimpleNamespace(_owner=_MetadataOwnerProxy(message._owner, recorder))
    return recorder.call('descriptor_fill_total', fill, observed, descriptor)


def install(runtime, recorder):
    """Patch only this isolated diagnostic peer, returning an explicit restoration scope."""
    from rclpy_hermos.types.publisher import TypedPublisher
    from rclpy_hermos_gpu.endpoint import publisher
    from rclpy_hermos_gpu.tensor import lease
    from experiments.g5.gpu_latency import dds_endpoint

    scope = ExitStack()

    def timed(target, name, stage):
        original = getattr(target, name)

        def observe(*args, **kwargs):
            return recorder.call(stage, original, *args, **kwargs)

        scope.enter_context(patch.object(target, name, observe))

    try:
        scope.enter_context(patch.object(runtime, '_native', NativeRuntimeProxy(runtime._native, recorder)))
        timed(TypedPublisher, 'borrow_message', 'descriptor_borrow')
        timed(TypedPublisher, 'publish', 'cpu_publish')
        fill = publisher.fill_descriptor
        scope.enter_context(patch.object(publisher, 'fill_descriptor',
                                         lambda message, descriptor: fill_with_costs(
                                             recorder, fill, message, descriptor)))
        # These modules retain imported aliases; patch the actual use sites.
        timed(lease, 'read_descriptor', 'descriptor_decode')
        timed(lease.TensorLease, 'as_tensor', 'tensor_export_total')
        timed(dds_endpoint, 'to_ros_message', 'descriptor_construct')
        timed(dds_endpoint, 'from_ros_message', 'descriptor_decode')
        publish = dds_endpoint.DDSPublisher.publish

        def dds_publish(endpoint, loan):
            original = endpoint._publisher
            endpoint._publisher = _PublisherProxy(original, recorder)
            try:
                return publish(endpoint, loan)
            finally:
                endpoint._publisher = original

        scope.enter_context(patch.object(dds_endpoint.DDSPublisher, 'publish', dds_publish))
    except BaseException:
        scope.close()
        raise
    return scope


def factory(runtime, arguments):
    """Reuse the normal peer and its GPU completion wait, saving a separate cost sidecar."""
    if len(arguments) != 3:
        raise ValueError('cost diagnostic requires role, config and output paths')
    config = json.loads(Path(arguments[1]).read_text())
    if config.get('timing_diagnostics') is not True or config.get('headline_eligible') is not False:
        raise ValueError('cost diagnostic must be explicitly excluded from headline timing')
    from experiments.g5.gpu_latency import peer

    recorder = Recorder()
    scope = install(runtime, recorder)

    def save(path, config, role):
        try:
            recorder.save(path, config, role)
        finally:
            scope.close()

    observer = SimpleNamespace(begin=recorder.begin, end=recorder.end,
                               call=recorder.call, save=save)
    try:
        return peer.factory(runtime, arguments, observer=observer)
    except BaseException:
        scope.close()
        raise
