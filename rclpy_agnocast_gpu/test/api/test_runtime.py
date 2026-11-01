"""Explicit initialization, registration and GPU runtime shutdown contracts."""

import sys

import pytest

from rclpy_agnocast_gpu import GpuRuntime
from rclpy_agnocast_gpu import runtime as runtime_module


def test_runtime_requires_explicit_initialisation_and_registration(runtime):
    with pytest.raises(RuntimeError, match='initialis'):
        runtime.register()
    runtime.initialise()
    with pytest.raises(RuntimeError, match='register'):
        runtime._check_admission()
    runtime.register()
    runtime._check_admission()


def test_second_process_runtime_is_rejected_after_first_shutdown(runtime):
    runtime.initialise()
    runtime.register()
    runtime.shutdown()
    other = GpuRuntime(block_count=0, block_bytes=4096, device=0,
                       max_reads=4, max_message_bytes=4096)
    with pytest.raises(RuntimeError, match='epoch'):
        other.initialise()


def test_registration_is_one_shot(runtime):
    runtime.initialise()
    runtime.register()
    with pytest.raises(RuntimeError, match='registered'):
        runtime.register()


def test_registration_records_actual_pid_and_complete_inventory(runtime, monkeypatch, tmp_path):
    import json
    import os
    manifest, ack = tmp_path / 'peer.json', tmp_path / 'ack'
    monkeypatch.setenv('G3_RECOVERY_MANIFEST', str(manifest))
    monkeypatch.setenv('G3_RECOVERY_ACK', str(ack))
    monkeypatch.setattr(runtime_module.time, 'sleep', lambda _: ack.touch())
    runtime.initialise()
    runtime.register()
    assert json.loads(manifest.read_text()) == {
        'version': 1, 'pid': os.getpid(), 'resources': runtime._native.paths}


def test_partial_registration_configuration_rejects_before_manifest(runtime, monkeypatch, tmp_path):
    manifest = tmp_path / 'peer.json'
    monkeypatch.setenv('G3_RECOVERY_MANIFEST', str(manifest))
    runtime.initialise()
    with pytest.raises(RuntimeError, match='together'):
        runtime.register()
    assert not manifest.exists()


def test_endpoint_close_is_attempted_before_runtime_drain_completion(registered_runtime):
    events = registered_runtime._native.events

    class Endpoint:
        def _stop_for_shutdown(self):
            events.append('stop')

        def close(self):
            events.append('endpoint-close')

    registered_runtime._endpoints.add(Endpoint())
    registered_runtime.shutdown()
    assert events.index('stop') < events.index('seal') < events.index('endpoint-close')


def test_busy_shutdown_remains_closed_to_new_work_and_reports_failure(registered_runtime):
    registered_runtime._native.close_ready = False
    with pytest.raises(RuntimeError, match='deadline'):
        registered_runtime.shutdown(timeout=0.001)
    with pytest.raises(RuntimeError, match='closing'):
        registered_runtime._check_admission()
    registered_runtime._native.close_ready = True
    registered_runtime.shutdown()


def test_poison_without_callbacks_propagates_through_poll(registered_runtime):
    registered_runtime._native.poisoned = True
    with pytest.raises(RuntimeError, match='poison'):
        registered_runtime.poll()


@pytest.mark.parametrize('field,value', [('block_count', -1), ('device', True),
                                        ('max_reads', 0), ('max_message_bytes', 0)])
def test_invalid_configuration_does_not_construct_native(field, value):
    config = dict(block_count=2, block_bytes=4096, device=0,
                  max_reads=4, max_message_bytes=4096)
    config[field] = value
    with pytest.raises((TypeError, ValueError)):
        GpuRuntime(**config)


@pytest.mark.parametrize('field', ['block_count', 'block_bytes', 'device', 'max_reads',
                                    'max_message_bytes'])
def test_runtime_configuration_cannot_change_after_construction(runtime, field):
    with pytest.raises(AttributeError):
        setattr(runtime, field, 10)


def test_failed_initialisation_cannot_register_or_expose_runtime(runtime):
    def fail():
        raise RuntimeError('initialisation failed')

    native_class = sys.modules['rclpy_agnocast_gpu._native'].Runtime
    original = native_class.initialise
    try:
        native_class.initialise = lambda self: fail()
        with pytest.raises(RuntimeError, match='failed'):
            runtime.initialise()
        with pytest.raises(RuntimeError, match='initialis'):
            runtime.register()
        with pytest.raises(RuntimeError, match='initialis'):
            runtime_module.current_runtime()
    finally:
        native_class.initialise = original


def test_busy_endpoint_prevents_producer_close(registered_runtime):

    class Endpoint:
        def _stop_for_shutdown(self):
            pass

        def close(self):
            raise RuntimeError('active CPU descriptor')

    registered_runtime._endpoints.add(Endpoint())
    with pytest.raises(RuntimeError, match='deadline'):
        registered_runtime.shutdown(timeout=0.001)
    assert 'seal' in registered_runtime._native.events
    assert 'drain' not in registered_runtime._native.events


def test_existing_ack_rejects_reusing_epoch(runtime, monkeypatch, tmp_path):
    ack = tmp_path / 'ack'
    ack.touch()
    monkeypatch.setenv('G3_RECOVERY_ACK', str(ack))
    monkeypatch.setenv('G3_RECOVERY_MANIFEST', str(tmp_path / 'manifest'))
    runtime.initialise()
    with pytest.raises(RuntimeError, match='new epoch'):
        runtime.register()


def test_shutdown_progress_can_release_busy_owners_and_is_not_called_after_drain(registered_runtime):
    registered_runtime._native.close_ready = False
    calls = []

    def progress():
        assert registered_runtime._closing and registered_runtime._native.closing
        calls.append('progress')
        registered_runtime._native.close_ready = True

    registered_runtime.shutdown(progress=progress)
    assert calls == ['progress']
    registered_runtime.shutdown(progress=lambda: pytest.fail('already closed'))


def test_shutdown_progress_exception_is_propagated_without_replacing_its_identity(registered_runtime):
    registered_runtime._native.close_ready = False
    error = LookupError('continuation failed')

    def progress():
        raise error

    with pytest.raises(LookupError) as caught:
        registered_runtime.shutdown(progress=progress)
    assert caught.value is error
    assert registered_runtime._closing and not registered_runtime._closed


def test_invalid_shutdown_progress_is_rejected_before_stopping_runtime(registered_runtime):
    with pytest.raises(TypeError, match='progress'):
        registered_runtime.shutdown(progress=1)
    assert not registered_runtime._closing and not registered_runtime._native.closing


