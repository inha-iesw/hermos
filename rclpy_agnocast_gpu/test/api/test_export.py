"""DLPack stream negotiation and capsule ownership failure contracts."""

import sys
from types import SimpleNamespace

import pytest

from rclpy_agnocast_gpu.tensor.export import StreamExport


def test_stream_mismatch_rejects_before_mapping_and_does_not_consume_export():
    calls = []
    native = SimpleNamespace(dlpack=lambda stream: calls.append(stream) or 'capsule')
    exporter = StreamExport(lambda: calls.append('map') or native, 0, 1234)
    assert exporter.__dlpack_device__() == (2, 0)
    with pytest.raises(ValueError, match='different'):
        exporter.__dlpack__(stream=5678)
    assert calls == []
    assert exporter.__dlpack__(stream=1234) == 'capsule'
    assert calls == ['map', 1234]
    with pytest.raises(BufferError, match='one'):
        exporter.__dlpack__(stream=1234)
    assert calls == ['map', 1234]


def test_dlpack_legacy_token_matches_torch_default_stream():
    exporter = StreamExport(lambda: SimpleNamespace(dlpack=lambda stream: stream), 0, 0)
    assert exporter.__dlpack__(stream=1) == 1


@pytest.mark.parametrize('kwargs', [{'copy': True}, {'dl_device': (2, 1)}])
def test_dlpack_copy_and_device_change_are_rejected_before_mapping(kwargs):
    exporter = StreamExport(lambda: pytest.fail('map must not run'), 0, 0)
    with pytest.raises(BufferError, match='does not copy'):
        exporter.__dlpack__(stream=1, **kwargs)


def test_unconsumed_export_cleanup_does_not_repeat_native_discard(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, 'rclpy_agnocast_gpu._native',
                        SimpleNamespace(discard_dlpack=lambda capsule: calls.append(capsule)))
    exporter = StreamExport(lambda: SimpleNamespace(dlpack=lambda _: 'capsule'), 0, 0)
    exporter.__dlpack__(stream=1, copy=False, max_version=(1, 0))
    exporter.discard_unconsumed()
    exporter.discard_unconsumed()
    assert calls == ['capsule']


def test_failed_capsule_export_releases_mapped_owner_even_when_traceback_is_saved(monkeypatch):
    from contextlib import nullcontext
    import weakref
    from rclpy_agnocast_gpu.tensor import export as export_module

    owners, destroyed = [], []

    class NativeRead:
        # Built-ins add no Python callee frame retaining self, matching a native
        # binding that throws while allocating its capsule.
        dlpack = staticmethod(memoryview)

        def __del__(self):
            destroyed.append(True)

    def map_read():
        native = NativeRead()
        owners.append(weakref.ref(native))
        return native

    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(device=lambda _: nullcontext(), stream=lambda _: nullcontext()),
        utils=SimpleNamespace(dlpack=SimpleNamespace(
            from_dlpack=lambda exporter, **kwargs: exporter.__dlpack__(stream=1, **kwargs))))
    monkeypatch.setitem(sys.modules, 'torch', fake_torch)
    monkeypatch.setattr(export_module, 'validate_torch_stream', lambda *_: 0)
    saved = None
    try:
        export_module.as_tensor(map_read, device=0, stream=object())
    except TypeError as error:
        saved = error
    assert saved is not None and saved.__traceback__ is not None
    assert len(owners) == 1
    assert owners[0]() is None
    assert destroyed == [True]

