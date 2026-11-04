"""Host-only checks of the selected module's device permission contract."""

import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import pytest

import system_runtime as runtime


def device_state(monkeypatch, *, mode='0660', node_mode=None, accessible=True,
                 numbers='240:0', name='hermos_rclpy'):
    metadata = SimpleNamespace(
        st_mode=stat.S_IFCHR | int(node_mode or mode, 8), st_rdev=os.makedev(240, 0))
    original_stat = Path.stat
    monkeypatch.setattr(Path, 'stat', lambda path, *args, **kwargs:
                        metadata if str(path) == '/dev/hermos_rclpy'
                        else original_stat(path, *args, **kwargs))
    monkeypatch.setattr(os, 'access', lambda *args: accessible)

    def read_text(path, **kwargs):
        if path.name == 'dev':
            return numbers
        assert path.name == 'uevent', path
        return f'DEVNAME={name}\nDEVMODE={mode}\n'

    monkeypatch.setattr(Path, 'read_text', read_text)


@pytest.mark.parametrize('mode', ['0660', '0666'])
def test_mode_comes_from_selected_artifact_not_module_name(monkeypatch, mode):
    device_state(monkeypatch, mode=mode)
    assert runtime._device_numbers(Path('/dev/hermos_rclpy'), 'hermos_rclpy', mode) == (240, 0)


@pytest.mark.parametrize('state,expected_error', [
    ({'mode': '0666'}, 'uevent'),
    ({'node_mode': '0666'}, 'mode does not match'),
    ({'accessible': False}, 'not readable and writable'),
    ({'numbers': '241:0'}, 'major/minor mismatch'),
    ({'name': 'other'}, 'uevent'),
])
def test_mismatched_or_inaccessible_device_is_rejected(monkeypatch, state, expected_error):
    device_state(monkeypatch, **state)
    with pytest.raises(runtime.PreflightError, match=expected_error):
        runtime._device_numbers(Path('/dev/hermos_rclpy'), 'hermos_rclpy', '0660')


@pytest.mark.parametrize('loaded', ['matching-version', 'other-version'])
def test_selected_module_must_match_loaded_module(monkeypatch, loaded):
    monkeypatch.setattr(Path, 'read_text', lambda *args, **kwargs: loaded)
    monkeypatch.setattr(runtime, '_module_info', lambda *args: 'matching-version')
    if loaded == 'matching-version':
        assert runtime._verify_kmod(Path('module.ko'), 'hermos_rclpy') == loaded.upper()
    else:
        with pytest.raises(runtime.PreflightError, match='srcversion mismatch'):
            runtime._verify_kmod(Path('module.ko'), 'hermos_rclpy')


@pytest.mark.parametrize('mode', ['0660', '0666', '', '0777'])
def test_preflight_requires_artifact_mode_before_device_access(monkeypatch, mode):
    monkeypatch.setattr(runtime, '_required_file', lambda variable, default: default)
    monkeypatch.setattr(runtime, '_subscriber_path', lambda: Path('/subscriber'))
    monkeypatch.setattr(runtime, '_verify_shm_rw', lambda: None)
    monkeypatch.setattr(runtime, '_verify_kmod', lambda *args: 'matching-version')
    fields = []

    def modinfo(command, **kwargs):
        assert command[:2] == ['modinfo', '-F']
        field = command[2]
        fields.append(field)
        return SimpleNamespace(returncode=0, stdout={'name': 'hermos_rclpy', 'devmode': mode}[field],
                               stderr='')

    observed = []
    monkeypatch.setattr(subprocess, 'run', modinfo)
    monkeypatch.setattr(runtime, '_device_numbers',
                        lambda *args: observed.append(args) or (240, 0))
    if mode in ('0660', '0666'):
        config = runtime.preflight()
        assert config.device_major == 240
        assert observed == [(Path('/dev/hermos_rclpy'), 'hermos_rclpy', mode)]
    else:
        with pytest.raises(runtime.PreflightError, match='devmode'):
            runtime.preflight()
        assert observed == []
    assert fields == ['name', 'devmode']
