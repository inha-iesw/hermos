"""Device identity and Python-origin failures are diagnosed before app startup."""

import os
from pathlib import Path
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
