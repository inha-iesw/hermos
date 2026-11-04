"""Installed CPU artifacts must be selected without loading native code."""

import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

import pytest

from hermos_runtime.environment import InstallationError, resolve_installation


@pytest.fixture
def installation(tmp_path, monkeypatch):
    prefixes = {}
    for name in ('hermos_runtime', 'hermoslib', 'rclpy_hermos', 'example_adapters'):
        prefix = tmp_path / name
        marker = prefix / 'share/ament_index/resource_index/packages' / name
        marker.parent.mkdir(parents=True)
        marker.write_text('')
        prefixes[name] = prefix
    artifacts = {}
    for role, package, relative in (
        ('library', 'hermoslib', 'lib/libhermos.so'),
        ('heaphook', 'hermoslib', 'lib/libhermos_heaphook.so'),
        ('registry', 'rclpy_hermos', 'lib/librclpy_hermos_types.so'),
        ('native', 'rclpy_hermos', 'lib/python3.10/site-packages/rclpy_hermos/_native.so'),
    ):
        path = prefixes[package] / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(role.encode())
        artifacts[role] = dict(package=package, path=relative,
                               sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    manifest = dict(manifest_version=1, ros_distro='humble', architecture=platform.machine(),
                    module='hermos_rclpy', device='/dev/hermos_rclpy',
                    adapter_abi=3, build_abi='test-abi', artifacts=artifacts)
    path = prefixes['hermos_runtime'] / 'share/hermos_runtime/cpu-manifest.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(manifest))
    provider = prefixes['example_adapters']
    resource = provider / 'share/ament_index/resource_index/rclpy_hermos_adapters/example_adapters'
    resource.parent.mkdir(parents=True)
    resource.write_text('share/example_adapters/manifest.json')
    provider_manifest = provider / resource.read_text()
    provider_manifest.parent.mkdir(parents=True)
    provider_manifest.write_text(json.dumps(dict(
        manifest_version=1, provider='example_adapters', adapter_abi=3,
        build_abi='test-abi', library='lib/provider.so')))
    (provider / 'lib').mkdir()
    (provider / 'lib/provider.so').write_bytes(b'provider')
    monkeypatch.setenv('AMENT_PREFIX_PATH', os.pathsep.join(map(str, prefixes.values())))
    monkeypatch.setenv('ROS_DISTRO', 'humble')
    return prefixes, path, manifest, provider_manifest


def test_resolves_relocated_isolated_prefixes(installation):
    prefixes, _, _, _ = installation
    result = resolve_installation(providers=['example_adapters'])
    assert result.heaphook == prefixes['hermoslib'] / 'lib/libhermos_heaphook.so'
    assert result.device == Path('/dev/hermos_rclpy')


@pytest.mark.parametrize('field,value', [
    ('manifest_version', 2), ('architecture', 'wrong-arch'), ('ros_distro', 'wrong-ros'),
    ('module', 'hermos'), ('device', '/dev/null'),
])
def test_rejects_incompatible_installation(installation, field, value):
    _, path, manifest, _ = installation
    manifest[field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(InstallationError):
        resolve_installation()


def test_rejects_stale_overlay_artifact(installation):
    prefixes, _, _, _ = installation
    (prefixes['hermoslib'] / 'lib/libhermos.so').write_bytes(b'stale')
    with pytest.raises(InstallationError, match='digest'):
        resolve_installation()


def test_rejects_manifest_path_escape(installation):
    _, path, manifest, _ = installation
    manifest['artifacts']['library']['path'] = '../../outside.so'
    path.write_text(json.dumps(manifest))
    with pytest.raises(InstallationError, match='relative'):
        resolve_installation()


def test_rejects_duplicate_manifest_keys(installation):
    _, path, _, _ = installation
    path.write_text('{"manifest_version":1,"manifest_version":1}')
    with pytest.raises(InstallationError, match='duplicate'):
        resolve_installation()


def test_missing_provider_fails_before_app_import(installation):
    with pytest.raises(InstallationError, match='missing.*provider'):
        resolve_installation(providers=['not_installed'])


def test_rejects_provider_abi_mismatch(installation):
    _, _, _, provider_path = installation
    provider = json.loads(provider_path.read_text())
    provider['build_abi'] = 'stale'
    provider_path.write_text(json.dumps(provider))
    with pytest.raises(InstallationError, match='ABI'):
        resolve_installation(providers=['example_adapters'])


def test_scoped_launch_environment_does_not_use_parent_overlay(installation, monkeypatch):
    _, _, _, _ = installation
    environment = dict(os.environ)
    monkeypatch.setenv('AMENT_PREFIX_PATH', '/missing/parent/overlay')
    monkeypatch.setenv('ROS_DISTRO', 'stale')
    result = resolve_installation(providers=['example_adapters'], environment=environment)
    assert result.manifest['ros_distro'] == 'humble'


def test_merged_install_resolves_all_packages_at_one_prefix(installation, tmp_path, monkeypatch):
    import shutil
    prefixes, _, _, _ = installation
    merged = tmp_path / 'merged'
    for prefix in prefixes.values():
        shutil.copytree(prefix, merged, dirs_exist_ok=True)
    monkeypatch.setenv('AMENT_PREFIX_PATH', str(merged))
    result = resolve_installation(providers=['example_adapters'])
    assert result.library == merged / 'lib/libhermos.so'


def test_resource_cannot_select_a_different_provider_prefix(installation, tmp_path, monkeypatch):
    _, _, _, _ = installation
    stale = tmp_path / 'stale'
    marker = stale / 'share/ament_index/resource_index/packages/example_adapters'
    marker.parent.mkdir(parents=True)
    marker.touch()
    monkeypatch.setenv('AMENT_PREFIX_PATH', str(stale) + os.pathsep + os.environ['AMENT_PREFIX_PATH'])
    with pytest.raises(InstallationError, match='mixed'):
        resolve_installation(providers=['example_adapters'])


def test_import_and_resolution_do_not_load_native_or_gpu(installation):
    result = subprocess.run([sys.executable, '-c', '''
import sys
from hermos_runtime.environment import resolve_installation
resolve_installation(providers=['example_adapters'])
assert not any(name.split('.')[0] in {
    'rclpy_hermos', 'rclpy_hermos_gpu', 'torch', 'launch', 'launch_ros'
} for name in sys.modules)
'''], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
