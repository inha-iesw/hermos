"""The installed observer delegates ACK and cleanup to the owned peer runner."""

import pytest
from types import SimpleNamespace

from hermos_runtime.cpu import registered as factory_runner
from hermos_runtime.observation import registered


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    monkeypatch.delenv('LD_PRELOAD', raising=False)
    monkeypatch.setenv('HERMOS_BRIDGE_MODE', 'off')
    monkeypatch.setenv('HERMOS_NO_DISCOVERY_AGENT', '1')
    monkeypatch.setenv('G3_RECOVERY_ROLE', 'metadata')
    monkeypatch.setenv('HERMOS_DEVICE_PATH', '')
    monkeypatch.delenv('HERMOS_DEVICE_PATH')
    monkeypatch.setattr(registered, 'resolve_installation',
                        lambda: SimpleNamespace(device='/dev/hermos_test'))
    monkeypatch.setattr(registered, 'check_device', lambda installation: None)


def test_observer_passes_ros_arguments_to_registered_factory(monkeypatch):
    calls = []
    monkeypatch.setattr(factory_runner, 'main', lambda args: calls.append(args) or 17)
    assert registered.main(['--ros-args', '-r', '__node:=observer']) == 17
    assert calls == [['ros2hermos_discovery_agent.observation:create',
                      '--ros-args', '-r', '__node:=observer']]
    assert registered.os.environ['HERMOS_DEVICE_PATH'] == '/dev/hermos_test'


def test_explicit_device_conflict_fails_before_initialization(monkeypatch):
    monkeypatch.setenv('HERMOS_DEVICE_PATH', '/dev/another_module')
    monkeypatch.setattr(factory_runner, 'main', lambda args: pytest.fail('started observer'))
    with pytest.raises(RuntimeError, match='HERMOS_DEVICE_PATH'):
        registered.main([])


@pytest.mark.parametrize('name,value', [
    ('LD_PRELOAD', '/some/heap.so'), ('HERMOS_BRIDGE_MODE', 'on'),
    ('HERMOS_NO_DISCOVERY_AGENT', '0'),
])
def test_unsafe_observer_environment_fails_before_initialization(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(factory_runner, 'main', lambda args: pytest.fail('started observer'))
    with pytest.raises(RuntimeError, match=name):
        registered.main([])


@pytest.mark.parametrize('role', [None, '', 'cpu', 'gpu'])
def test_metadata_entry_requires_supervisor_metadata_role_before_initialization(monkeypatch, role):
    if role is None:
        monkeypatch.delenv('G3_RECOVERY_ROLE')
    else:
        monkeypatch.setenv('G3_RECOVERY_ROLE', role)
    monkeypatch.setattr(registered, 'resolve_installation',
                        lambda: pytest.fail('resolved installation before role validation'))
    monkeypatch.setattr(factory_runner, 'main', lambda args: pytest.fail('started observer'))
    with pytest.raises(RuntimeError, match='G3_RECOVERY_ROLE=metadata'):
        registered.main([])


def test_help_does_not_initialize_ros_or_require_registration(monkeypatch, capsys):
    monkeypatch.delenv('G3_RECOVERY_ROLE')
    monkeypatch.setattr(factory_runner, 'main', lambda args: pytest.fail('started observer'))
    with pytest.raises(SystemExit) as error:
        registered.main(['--help'])
    assert error.value.code == 0
    assert 'metadata' in capsys.readouterr().out
