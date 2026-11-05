"""Host oracles for installed artifacts and real pipeline library evidence."""

from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import platform
import shutil
from types import SimpleNamespace

import pytest

from hermos_gpu_examples.pipeline.peer import node_proof
from hermos_runtime.environment import InstallationError
import installation
import test_installed as device_tests


@pytest.fixture
def session_installation(tmp_path):
    prefix = tmp_path / 'installed'
    packages = prefix / 'share/ament_index/resource_index/packages'
    packages.mkdir(parents=True)
    for package in ('hermos_runtime', 'hermoslib', 'rclpy_hermos', 'rclpy_hermos_adapters'):
        (packages / package).touch()
    artifacts = {}
    for role, package, relative in (
        ('library', 'hermoslib', 'lib/libhermos.so'),
        ('heaphook', 'hermoslib', 'lib/libhermos_heaphook.so'),
        ('registry', 'rclpy_hermos', 'lib/librclpy_hermos_types.so'),
        ('native', 'rclpy_hermos', 'lib/python3.10/site-packages/rclpy_hermos/_native.so'),
    ):
        path = prefix / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(role.encode())
        artifacts[role] = dict(package=package, path=relative,
                               sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    manifest = prefix / 'share/hermos_runtime/cpu-manifest.json'
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(dict(
        manifest_version=1, ros_distro='humble', architecture=platform.machine(),
        module='hermos_rclpy', device='/dev/hermos_rclpy', adapter_abi=1,
        build_abi='pipeline-test', artifacts=artifacts)))
    provider = prefix / 'share/rclpy_hermos_adapters/manifest.json'
    provider.parent.mkdir(parents=True)
    provider.write_text(json.dumps(dict(
        manifest_version=1, provider='rclpy_hermos_adapters', adapter_abi=1,
        build_abi='pipeline-test', library='lib/provider.so')))
    (prefix / 'lib/provider.so').write_bytes(b'provider')
    resource = prefix / 'share/ament_index/resource_index/rclpy_hermos_adapters/rclpy_hermos_adapters'
    resource.parent.mkdir(parents=True)
    resource.write_text(str(provider.relative_to(prefix)))
    bundle = tmp_path / 'bundle'
    (bundle / 'lib').mkdir(parents=True)
    entries = {}
    for role in ('library', 'heaphook'):
        relative = artifacts[role]['path']
        shutil.copyfile(prefix / relative, bundle / relative)
        entries[relative] = dict(sha256=artifacts[role]['sha256'])
    (bundle / 'artifacts.json').write_text(json.dumps(dict(artifacts=entries)))
    module = bundle / 'kmod/hermos_rclpy.ko'
    module.parent.mkdir()
    module.write_bytes(b'module')
    return SimpleNamespace(prefix=prefix, manifest=manifest, bundle=bundle, module=module,
                           environment=dict(AMENT_PREFIX_PATH=str(prefix), ROS_DISTRO='humble'))


def test_session_guard_accepts_matching_selected_child_installation(session_installation, monkeypatch):
    selected = session_installation
    monkeypatch.setenv('AMENT_PREFIX_PATH', '/missing/parent/overlay')
    monkeypatch.setenv('ROS_DISTRO', 'wrong-parent-distro')
    installation.verify_session_installation(selected.environment, selected.module)


@pytest.mark.parametrize('role', ['heaphook', 'library'])
def test_session_guard_rejects_valid_installation_from_another_bundle(
        session_installation, monkeypatch, tmp_path, role):
    selected = session_installation
    parent = tmp_path / 'parent-installation'
    shutil.copytree(selected.prefix, parent)
    monkeypatch.setenv('AMENT_PREFIX_PATH', str(parent))
    monkeypatch.setenv('ROS_DISTRO', 'humble')
    manifest = json.loads(selected.manifest.read_text())
    entry = manifest['artifacts'][role]
    (selected.prefix / entry['path']).write_bytes(b'other build')
    entry['sha256'] = hashlib.sha256(b'other build').hexdigest()
    selected.manifest.write_text(json.dumps(manifest))
    with pytest.raises(AssertionError, match='installed artifact differs'):
        installation.verify_session_installation(selected.environment, selected.module)


@pytest.mark.parametrize('damage', ['missing-role', 'changed-bytes'])
def test_session_guard_rejects_incomplete_or_changed_bundle(session_installation, damage):
    selected = session_installation
    if damage == 'missing-role':
        manifest = selected.bundle / 'artifacts.json'
        data = json.loads(manifest.read_text())
        del data['artifacts']['lib/libhermos_heaphook.so']
        manifest.write_text(json.dumps(data))
        message = 'bundle lacks'
    else:
        (selected.bundle / 'lib/libhermos.so').write_bytes(b'changed after verification')
        message = 'bundle artifact changed'
    with pytest.raises(AssertionError, match=message):
        installation.verify_session_installation(selected.environment, selected.module)


@pytest.mark.parametrize('role', ['native', 'registry'])
def test_session_guard_also_validates_other_installed_cpu_artifacts(session_installation, role):
    selected = session_installation
    manifest = json.loads(selected.manifest.read_text())
    (selected.prefix / manifest['artifacts'][role]['path']).write_bytes(b'stale')
    with pytest.raises(InstallationError, match='artifact digest mismatch: ' + role):
        installation.verify_session_installation(selected.environment, selected.module)


@pytest.mark.parametrize('source_free', [False, True], ids=['installed', 'source-free'])
def test_device_driver_rejects_bundle_mismatch_before_launch(
        session_installation, monkeypatch, tmp_path, source_free):
    selected = session_installation
    (selected.bundle / 'lib/libhermos.so').write_bytes(b'changed after verification')
    for key, value in selected.environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv('RCLPY_HERMOS_KMOD', str(selected.module))
    monkeypatch.setenv('G3_PEER_LOG_ROOT', str(tmp_path))
    # The source-free child receives no bundle/module path; the parent owns it.
    monkeypatch.setattr(device_tests, 'relocated_installation', lambda: nullcontext(
        (selected.prefix, selected.environment)))

    def forbidden(*args, **kwargs):
        pytest.fail('pipeline started before rejecting mismatched device artifacts')

    monkeypatch.setattr(device_tests.subprocess, 'Popen', forbidden)
    with pytest.raises(AssertionError, match='bundle artifact changed'):
        device_tests.test_installed_gpu_launch_preserves_payload_and_dds_coexistence(source_free)


def test_library_proof_excludes_shm_but_keeps_workspace_and_native_provider_libraries(monkeypatch):
    paths = [
        '/dev/shm/hermos@123',
        '/dev/shm/hermos@456 (deleted)',
        '/workspace/wrong/hermoslib/lib/libhermos.so',
        '/tmp/relocated/rclpy_hermos/_native.cpython-310-aarch64-linux-gnu.so',
        '/tmp/relocated/lib/librclpy_hermos_types.so.1',
        '/usr/lib/aarch64-linux-gnu/libc.so.6',
    ]
    maps = '\n'.join(f'1000-2000 r--p 00000000 00:00 1 {path}' for path in paths)
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        return maps if path == Path('/proc/self/maps') else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read_text)
    node = SimpleNamespace(get_name=lambda: 'peer', get_namespace=lambda: '/camera',
                           resolve_topic_name=lambda topic: '/camera/' + topic,
                           get_parameter=lambda name: SimpleNamespace(value='proof'))
    proof = node_proof(node, 'frames')
    assert proof['mapped_libraries'] == sorted(paths[2:5])
