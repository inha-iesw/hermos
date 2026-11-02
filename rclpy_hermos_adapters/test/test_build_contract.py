"""Exercise installed adapter build rules through independent temporary packages."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest
from ament_index_python.packages import get_package_prefix, get_package_share_directory


_PROVIDER = 'build_contract_provider'


def _run(command, *, env=None, timeout=45, expected_returncode=0):
    result = subprocess.run(
        [str(argument) for argument in command], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
    assert result.returncode == expected_returncode, (
        f'Command {command!r} returned {result.returncode}:\n{result.stdout}')
    return result.stdout


def _write_project(source, *, types=('std_msgs/msg/Int32',), target='_types', debug=False):
    source.mkdir(exist_ok=True)
    (source / 'package.xml').write_text(textwrap.dedent(f'''\
        <?xml version="1.0"?>
        <package format="3">
          <name>{_PROVIDER}</name>
          <version>0.1.0</version>
          <description>Temporary adapter build contract fixture.</description>
          <maintainer email="maintainers@example.com">Maintainers</maintainer>
          <license>Apache-2.0</license>
          <buildtool_depend>ament_cmake</buildtool_depend>
          <buildtool_depend>rclpy_hermos_codegen</buildtool_depend>
          <depend>std_msgs</depend>
          <export><build_type>ament_cmake</build_type></export>
        </package>
        '''), encoding='utf-8')
    debug_definition = (
        f'target_compile_definitions({target} PRIVATE _GLIBCXX_DEBUG)\n' if debug else '')
    (source / 'CMakeLists.txt').write_text(textwrap.dedent(f'''\
        cmake_minimum_required(VERSION 3.14)
        project({_PROVIDER})
        find_package(ament_cmake REQUIRED)
        find_package(rclpy_hermos_codegen REQUIRED)
        rclpy_hermos_generate_adapters({target} TYPES {' '.join(types)})
        ''') + debug_definition + 'ament_package()\n', encoding='utf-8')


def _configure(source, build, *, env=None, hermos_prefix=None):
    return _run([
        'cmake', '-S', source, '-B', build, '-G', 'Unix Makefiles',
        f'-DPython3_EXECUTABLE={sys.executable}', '-DCMAKE_BUILD_TYPE=Debug',
        *([f'-Dhermoslib_DIR={hermos_prefix}/share/hermoslib/cmake']
          if hermos_prefix else [])], env=env)


def _build(build, *, env=None):
    _run(['cmake', '--build', build, '--parallel', '2'], env=env, timeout=60)


def _snapshot(source):
    return {
        str(path.relative_to(source)): path.read_bytes()
        for path in source.rglob('*') if path.is_file()}


def _module_path(build, target):
    libraries = list(build.glob(f'{target}*.so'))
    assert len(libraries) == 1, libraries
    return libraries[0]


def _header_digest(root):
    identity = ''.join(
        f'{path.relative_to(root).as_posix()}:{hashlib.sha256(path.read_bytes()).hexdigest()}\n'
        for path in sorted(root.rglob('*')) if path.suffix in ('.h', '.hpp'))
    return hashlib.sha256(identity.encode()).hexdigest()


def test_type_selection_rebuilds_manifest_without_source_writes_or_overlay_generator(tmp_path):
    """Regenerate changed TYPES, preserve sources, and ignore a shadow Python generator."""
    source, build = tmp_path / 'source', tmp_path / 'build'
    shadow = tmp_path / 'shadow' / 'rclpy_hermos_codegen'
    shadow.mkdir(parents=True)
    (shadow / '__init__.py').write_text(
        'raise RuntimeError("shadow generator must not be imported")\n', encoding='utf-8')
    environment = os.environ.copy()
    environment['PYTHONPATH'] = (
        str(shadow.parent) + os.pathsep + environment.get('PYTHONPATH', ''))
    _write_project(source)
    authored = _snapshot(source)
    _configure(source, build, env=environment)
    _build(build, env=environment)
    manifest = build / '_types' / 'manifest.json'
    assert set(json.loads(manifest.read_text())['types']) == {'std_msgs/msg/Int32'}
    assert _snapshot(source) == authored

    cmake = source / 'CMakeLists.txt'
    cmake.write_text(
        cmake.read_text().replace(
            'TYPES std_msgs/msg/Int32)', 'TYPES std_msgs/msg/Int32 std_msgs/msg/Float32)'),
        encoding='utf-8')
    authored['CMakeLists.txt'] = cmake.read_bytes()
    _configure(source, build, env=environment)
    _build(build, env=environment)
    assert set(json.loads(manifest.read_text())['types']) == {
        'std_msgs/msg/Int32', 'std_msgs/msg/Float32'}
    assert _snapshot(source) == authored

    outputs = [manifest, build / '_types' / 'provider.cpp', _module_path(build, '_types')]
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in outputs}
    _build(build, env=environment)
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in outputs} == before
    assert _snapshot(source) == authored


def test_debug_stl_factory_is_rejected_by_loader_before_registration(tmp_path):
    """Reject the debug-STL Hermos symbol before PyInit without changing the registry."""
    source, build = tmp_path / 'source', tmp_path / 'build'
    _write_project(source, target='_bad', debug=True)
    _configure(source, build)
    _build(build)
    manifest = json.loads((build / '_bad' / 'manifest.json').read_text())
    assert ';libstdc++debug=0' in manifest['build_abi']
    probe = textwrap.dedent('''\
        import importlib.util
        import sys
        from rclpy_hermos import _native

        def assert_unregistered():
            try:
                _native._registered_type('std_msgs/msg/Int32')
            except IndexError:
                return
            raise AssertionError('incompatible plugin registered a type')

        assert_unregistered()
        spec = importlib.util.spec_from_file_location(
            'build_contract_provider._bad', sys.argv[1])
        try:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except ImportError as error:
            detail = str(error)
            assert 'undefined symbol: _ZN6hermos12publish_core' in detail, detail
            assert 'NSt7__debug13unordered_map' in detail, detail
        else:
            raise AssertionError('incompatible plugin imported successfully')
        assert_unregistered()
        print('ABI_REJECTED_REGISTRY_UNCHANGED')
        ''')
    output = _run([sys.executable, '-c', probe, _module_path(build, '_bad')], timeout=15)
    assert 'ABI_REJECTED_REGISTRY_UNCHANGED' in output


@pytest.mark.parametrize('mismatch', [None, 'ros', 'types', 'hermos'])
def test_generated_module_checks_own_abi_tag_after_successful_dlopen(tmp_path, mismatch):
    """Check link-compatible module-specific ROS, types and Hermos header identities."""
    source, build = tmp_path / 'source', tmp_path / 'build'
    _write_project(source, target='_handshake')
    header = source / 'module_abi.hpp'
    macros = {
        'ros': 'RCLPY_HERMOS_ROS_ABI',
        'types': 'RCLPY_HERMOS_TYPES_HEADERS_SHA256',
        'hermos': 'RCLPY_HERMOS_SELECTED_HERMOS_HEADERS_SHA256',
    }
    header.write_text(
        f'#undef {macros[mismatch]}\n'
        f'#define {macros[mismatch]} "incompatible-test-identity"\n'
        if mismatch else '#pragma once\n', encoding='utf-8')
    cmake = source / 'CMakeLists.txt'
    cmake.write_text(cmake.read_text().replace(
        'ament_package()',
        'target_compile_options(_handshake PRIVATE '
        '-include "${CMAKE_CURRENT_SOURCE_DIR}/module_abi.hpp")\nament_package()'))
    _configure(source, build)
    _build(build)
    manifest = build / '_handshake' / 'manifest.json'
    probe = textwrap.dedent('''\
        import ctypes
        import hashlib
        import importlib.util
        import json
        from pathlib import Path
        import sys
        from rclpy_hermos import _native

        def assert_unregistered():
            try:
                _native._registered_type('std_msgs/msg/Int32')
            except IndexError:
                return
            raise AssertionError('plugin registered before the handshake completed')

        manifest_bytes = Path(sys.argv[2]).read_bytes()
        assert json.loads(manifest_bytes)['build_abi'] == _native._adapter_abi()['build']
        assert_unregistered()
        loaded = ctypes.CDLL(sys.argv[1])
        print('DLOPEN_SUCCEEDED')
        spec = importlib.util.spec_from_file_location(
            'build_contract_provider._handshake', sys.argv[1])
        if sys.argv[3] == 'mismatch':
            try:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except ImportError as error:
                assert str(error) == 'adapter ABI mismatch', str(error)
            else:
                raise AssertionError('module-specific ABI mismatch was accepted')
            assert_unregistered()
            print('HANDSHAKE_REJECTED_REGISTRY_UNCHANGED')
        else:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            assert_unregistered()
            module._register(hashlib.sha256(manifest_bytes).hexdigest())
            assert _native._registered_type('std_msgs/msg/Int32')['native_size'] > 0
            print('MATCHING_HANDSHAKE_REGISTERED')
        ''')
    output = _run([
        sys.executable, '-c', probe, _module_path(build, '_handshake'), manifest,
        'mismatch' if mismatch else 'matching'], timeout=15)
    assert 'DLOPEN_SUCCEEDED' in output
    assert ('HANDSHAKE_REJECTED_REGISTRY_UNCHANGED' if mismatch
            else 'MATCHING_HANDSHAKE_REGISTERED') in output


def test_header_identity_reconfigures_for_content_addition_and_removal(tmp_path):
    """Track header edits on build alone using a prefix-independent content oracle."""
    source, build = tmp_path / 'source', tmp_path / 'build'
    headers = source / 'headers'
    headers.mkdir(parents=True)
    first = headers / 'one.hpp'
    first.write_text('// Initial published contract.\n')
    identity_helper = (
        Path(get_package_share_directory('rclpy_hermos')) / 'cmake' / 'adapter_identity.cmake')
    (source / 'CMakeLists.txt').write_text(textwrap.dedent(f'''\
        cmake_minimum_required(VERSION 3.14)
        project(header_identity NONE)
        include("{identity_helper}")
        _rclpy_hermos_headers_digest(digest "${{CMAKE_CURRENT_SOURCE_DIR}}/headers")
        file(WRITE "${{CMAKE_CURRENT_BINARY_DIR}}/digest.txt" "${{digest}}")
        add_custom_target(identity ALL)
        '''))
    _run(['cmake', '-S', source, '-B', build, '-G', 'Unix Makefiles'])

    def checked_digest():
        _build(build)
        digest = (build / 'digest.txt').read_text()
        assert digest == _header_digest(headers)
        return digest

    initial = checked_digest()
    first.write_text('// Changed published contract.\n')
    changed = checked_digest()
    assert changed != initial
    nested = headers / 'nested' / 'two.h'
    nested.parent.mkdir()
    nested.write_text('// Additional published contract.\n')
    added = checked_digest()
    assert added != changed
    nested.unlink()
    assert checked_digest() == changed


@pytest.mark.parametrize('changed_headers', [False, True])
def test_selected_hermos_header_identity_is_independent_of_runtime_prefix(
        tmp_path, changed_headers):
    """Accept a byte-identical relocation and reject changed headers at both gates."""
    original = Path(get_package_prefix('hermoslib'))
    selected = tmp_path / 'hermoslib'
    shutil.copytree(original / 'include', selected / 'include')
    shutil.copytree(original / 'share' / 'hermoslib', selected / 'share' / 'hermoslib')
    (selected / 'lib').mkdir()
    (selected / 'lib' / 'libhermos.so').symlink_to(original / 'lib' / 'libhermos.so')
    headers = selected / 'include' / 'hermos'
    if changed_headers:
        header = headers / 'hermos_publisher.hpp'
        header.write_text(header.read_text() + '\n// Distinct published inline contract.\n')
    source, build = tmp_path / 'source', tmp_path / 'build'
    _write_project(source)
    _configure(source, build, hermos_prefix=selected)
    _build(build)

    manifest = json.loads((build / '_types' / 'manifest.json').read_text())
    axes = dict(axis.split('=', 1) for axis in manifest['build_abi'].split(';'))
    published = Path(get_package_prefix('rclpy_hermos')) / 'include' / 'rclpy_hermos' / 'types'
    assert axes['types'] == _header_digest(published)
    assert axes['hermos'] == _header_digest(headers)
    assert (axes['hermos'] != _header_digest(original / 'include' / 'hermos')) == (
        changed_headers)
    provider = tmp_path / 'provider'
    resource = Path('share') / _PROVIDER / 'adapters' / 'manifest.json'
    (provider / resource).parent.mkdir(parents=True)
    shutil.copyfile(build / '_types' / 'manifest.json', provider / resource)
    library = provider / manifest['library']
    library.parent.mkdir(parents=True)
    library.symlink_to(_module_path(build, '_types'))
    probe = textwrap.dedent('''\
        import ctypes
        import importlib.util
        import json
        from pathlib import Path
        import sys
        from rclpy_hermos import _native
        from rclpy_hermos.types import discovery

        provider, resource, library = sys.argv[1:4]
        mismatch = sys.argv[4] == 'mismatch'
        manifest = json.loads((Path(provider) / resource).read_text())
        runtime = dict(axis.split('=', 1) for axis in _native._adapter_abi()['build'].split(';'))
        generated = dict(axis.split('=', 1) for axis in manifest['build_abi'].split(';'))
        assert generated['types'] == runtime['types']
        assert (generated['hermos'] != runtime['hermos']) == mismatch
        discovery.get_resources = lambda _: {'build_contract_provider': provider}
        discovery.get_resource = lambda *_: (resource, provider)

        def assert_unregistered():
            try:
                _native._registered_type('std_msgs/msg/Int32')
            except IndexError:
                return
            raise AssertionError('incompatible provider registered a type')

        assert_unregistered()
        if mismatch:
            def unexpected_load(*_):
                raise AssertionError('discovery attempted to import incompatible provider')
            discovery._load_plugin = unexpected_load
            try:
                discovery._resolve_type('std_msgs/msg/Int32')
            except ValueError as error:
                assert str(error) == 'adapter ABI mismatch: std_msgs/msg/Int32; rebuild adapter'
            else:
                raise AssertionError('discovery accepted incompatible selected headers')
            assert_unregistered()
            loaded = ctypes.CDLL(library)
            spec = importlib.util.spec_from_file_location(manifest['module'], library)
            try:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except ImportError as error:
                assert str(error) == 'adapter ABI mismatch', str(error)
            else:
                raise AssertionError('C handshake accepted incompatible selected headers')
            assert_unregistered()
            print('SELECTED_HEADERS_REJECTED_AT_BOTH_GATES')
        else:
            assert discovery._resolve_type('std_msgs/msg/Int32')['native_size'] > 0
            print('RELOCATED_HEADERS_REGISTERED')
        ''')
    output = _run([
        sys.executable, '-c', probe, provider, resource, library,
        'mismatch' if changed_headers else 'matching'], timeout=15)
    assert ('SELECTED_HEADERS_REJECTED_AT_BOTH_GATES' if changed_headers
            else 'RELOCATED_HEADERS_REGISTERED') in output


def test_in_source_configuration_fails_before_adapter_output(tmp_path):
    """Reject in-source configuration without generating provider artifacts."""
    source = tmp_path / 'source'
    _write_project(source)
    output = _run([
        'cmake', '-S', source, '-B', source,
        f'-DPython3_EXECUTABLE={sys.executable}'], expected_returncode=1)
    assert 'requires an out-of-source build' in output
    assert not (source / '_types').exists()
    assert not list(source.rglob('provider.cpp'))
    assert not list(source.rglob('manifest.json'))


@pytest.mark.parametrize('in_subdirectory', [False, True])
def test_second_provider_in_same_package_is_rejected(tmp_path, in_subdirectory):
    """Reject duplicate provider ownership even across CMake directory scopes."""
    source, build = tmp_path / 'source', tmp_path / 'build'
    _write_project(source)
    second = 'rclpy_hermos_generate_adapters(_other TYPES std_msgs/msg/Float32)\n'
    if in_subdirectory:
        nested = source / 'nested'
        nested.mkdir()
        (nested / 'CMakeLists.txt').write_text(second)
        second = 'add_subdirectory(nested)\n'
    cmake = source / 'CMakeLists.txt'
    cmake.write_text(cmake.read_text().replace('ament_package()', second + 'ament_package()'))
    output = _run([
        'cmake', '-S', source, '-B', build,
        f'-DPython3_EXECUTABLE={sys.executable}'], expected_returncode=1)
    assert 'Only one adapter provider per package is supported' in output
