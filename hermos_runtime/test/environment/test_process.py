"""Device identity and Python-origin failures are diagnosed before app startup."""

import os
import importlib.machinery
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from hermos_runtime.environment import Installation, InstallationError
from hermos_runtime.environment import process


@pytest.fixture
def device(monkeypatch):
    installed = Installation(Path('/hook'), Path('/lib'), Path('/dev/hermos_rclpy'), {},
                             dict(module='hermos_rclpy'))
    monkeypatch.setattr(Path, 'is_dir', lambda self: self == Path('/sys/module/hermos_rclpy'))
    null_stat = Path('/dev/null').stat()
    monkeypatch.setattr(Path, 'stat', lambda self, **kwargs: null_stat)
    monkeypatch.setattr(os, 'access', lambda *args: True)
    expected = Path('/sys/class/hermos_rclpy_class/hermos_rclpy/dev')
    def read(self, *args, **kwargs):
        assert self == expected
        return f'{os.major(null_stat.st_rdev)}:{os.minor(null_stat.st_rdev)}\n'
    monkeypatch.setattr(Path, 'read_text', read)
    return installed


def test_checks_actual_backend_class_device_identity(device):
    process.check_device(device)


def test_reports_missing_module_separately(device, monkeypatch):
    monkeypatch.setattr(Path, 'is_dir', lambda self: False)
    with pytest.raises(InstallationError, match='missing kernel module'):
        process.check_device(device)


def test_reports_device_access_denial(device, monkeypatch):
    monkeypatch.setattr(os, 'access', lambda *args: False)
    with pytest.raises(InstallationError, match='permission denied'):
        process.check_device(device)


def test_rejects_device_of_a_different_driver(device, monkeypatch):
    monkeypatch.setattr(Path, 'read_text', lambda *args, **kwargs: '999:999')
    with pytest.raises(InstallationError, match='identity mismatch'):
        process.check_device(device)


def test_rejects_python_package_from_stale_overlay_without_import(monkeypatch, tmp_path):
    selected = tmp_path / 'selected/_native.so'
    stale = tmp_path / 'stale/_native.so'
    installed = Installation(Path('/hook'), Path('/lib'), Path('/dev/hermos_rclpy'),
                             dict(native=selected), {})
    def spec(name, search=None):
        if search is None:
            return SimpleNamespace(submodule_search_locations=[str(stale.parent)])
        return SimpleNamespace(origin=str(stale))
    monkeypatch.setattr(process.importlib.machinery.PathFinder, 'find_spec', spec)
    with pytest.raises(InstallationError, match='Python/native prefix mismatch'):
        process.check_python_origin(installed)


@pytest.fixture
def factory_installation(tmp_path):
    native_name = '_native' + importlib.machinery.EXTENSION_SUFFIXES[0]
    prefixes = [tmp_path / name for name in ('selected', 'stale')]
    for prefix in prefixes:
        package = prefix / 'rclpy_hermos'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('raise AssertionError("payload package imported")\n')
        # PathFinder must inspect this path without loading its deliberately invalid ELF.
        (package / native_name).write_text('not a loadable native library\n')
    script = tmp_path / 'installed/lib/hermos_runtime/hermos_cpu_peer'
    script.parent.mkdir(parents=True)
    script.write_text('#!/usr/bin/python3\nraise AssertionError("peer application executed")\n')
    native = prefixes[0] / 'rclpy_hermos' / native_name
    installation = Installation(Path('/hook'), Path('/lib'), Path('/dev/hermos_rclpy'),
                                dict(native=native), {})
    return installation, script, prefixes


def test_factory_origin_uses_child_pythonpath_instead_of_parent(factory_installation, monkeypatch):
    installation, script, (selected, stale) = factory_installation
    monkeypatch.syspath_prepend(str(stale))
    environment = dict(PATH='/different/child/path', PYTHONPATH=str(selected),
                       LD_PRELOAD='/unused/payload-hook.so')
    process.check_factory_python_origin(installation, script, environment)
    assert environment['LD_PRELOAD'] == '/unused/payload-hook.so'


def test_factory_origin_rejects_child_stale_overlay_even_when_parent_is_correct(
        factory_installation, monkeypatch):
    installation, script, (selected, stale) = factory_installation
    monkeypatch.syspath_prepend(str(selected))
    with pytest.raises(InstallationError, match='Python/native prefix mismatch'):
        process.check_factory_python_origin(installation, script,
                                            dict(PATH='/usr/bin', PYTHONPATH=str(stale)))


def test_factory_origin_uses_resolved_script_directory_and_not_cwd(
        factory_installation, tmp_path, monkeypatch):
    installation, script, (selected, stale) = factory_installation
    monkeypatch.chdir(stale)
    link = tmp_path / 'entry-link'
    link.symlink_to(script)
    process.check_factory_python_origin(installation, link,
                                        dict(PATH='/usr/bin', PYTHONPATH=str(selected)))
    # A package beside the real script takes precedence, including through a symlink.
    (script.parent / 'rclpy_hermos').symlink_to(stale / 'rclpy_hermos', target_is_directory=True)
    with pytest.raises(InstallationError, match='Python/native prefix mismatch'):
        process.check_factory_python_origin(installation, link,
                                            dict(PATH='/usr/bin', PYTHONPATH=str(selected)))


def test_factory_origin_honors_child_pythonhome_failure(factory_installation, tmp_path):
    installation, script, (selected, _) = factory_installation
    with pytest.raises(InstallationError, match='factory Python environment'):
        process.check_factory_python_origin(installation, script,
            dict(PATH='/usr/bin', PYTHONPATH=str(selected), PYTHONHOME=str(tmp_path / 'missing-home')))


def test_factory_origin_inspector_has_no_preload_and_times_out(factory_installation, monkeypatch):
    installation, script, (selected, _) = factory_installation
    environment = dict(PATH='/selected/bin', PYTHONPATH=str(selected),
                       PYTHONHOME='/selected/home', LD_PRELOAD='/payload-hook.so')

    def timeout(command, **kwargs):
        assert kwargs['env'] == {key: value for key, value in environment.items() if key != 'LD_PRELOAD'}
        assert 0 < kwargs['timeout'] <= 10
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])

    monkeypatch.setattr(subprocess, 'run', timeout)
    with pytest.raises(InstallationError, match='timed out'):
        process.check_factory_python_origin(installation, script, environment)
