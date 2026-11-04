"""Versioned, relocatable identities for the selected ament installation."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform

from ament_index_python.resources import get_resource


class InstallationError(RuntimeError):
    """An installation cannot safely start a HERM0S peer."""


@dataclass(frozen=True)
class Installation:
    """Verified paths and metadata; no shared library has been loaded."""

    heaphook: Path
    library: Path
    device: Path
    artifacts: dict
    manifest: dict


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InstallationError(f'duplicate manifest key: {key}')
        result[key] = value
    return result


def read_json(path):
    """Read an object, rejecting ambiguous keys and reporting its location."""
    try:
        result = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique)
    except (OSError, ValueError) as error:
        raise InstallationError(f'cannot read installation manifest {path}: {error}') from error
    if not isinstance(result, dict):
        raise InstallationError(f'manifest must be an object: {path}')
    return result


def relative_path(value):
    """Validate a manifest path before joining it to an ament-selected prefix."""
    if not isinstance(value, str):
        raise InstallationError('artifact path must be relative text')
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or '..' in path.parts or '\\' in value:
        raise InstallationError(f'artifact path must be relative: {value!r}')
    return path


def _resource(kind, name, environment):
    if environment is None:
        return get_resource(kind, name)
    if not name or '/' in name or '\\' in name:
        raise InstallationError(f'invalid package/provider name: {name!r}')
    for prefix in environment.get('AMENT_PREFIX_PATH', '').split(os.pathsep):
        if not prefix:
            continue
        resource = Path(prefix) / 'share/ament_index/resource_index' / kind / name
        if resource.is_file():
            return resource.read_text(), prefix
    raise LookupError(name)


def _prefix(package, environment):
    try:
        return Path(_resource('packages', package, environment)[1])
    except LookupError as error:
        raise InstallationError(f'missing installed package: {package}') from error


def _verify_provider(provider, manifest, environment):
    try:
        resource, selected = _resource('rclpy_hermos_adapters', provider, environment)
    except LookupError as error:
        raise InstallationError(f'missing adapter provider: {provider}') from error
    prefix = _prefix(provider, environment)
    if prefix != Path(selected):
        raise InstallationError(f'mixed package/provider prefixes: {provider}')
    data = read_json(prefix / relative_path(resource.strip()))
    if data.get('manifest_version') != 1 or data.get('provider') != provider:
        raise InstallationError(f'invalid adapter provider manifest: {provider}')
    if any(data.get(key) != manifest[key] for key in ('adapter_abi', 'build_abi')):
        raise InstallationError(f'adapter ABI mismatch: {provider}; rebuild the selected overlay')
    library = prefix / relative_path(data.get('library'))
    if not library.is_file():
        raise InstallationError(f'missing adapter provider library: {library}')


def resolve_installation(*, providers=(), environment=None):
    """Verify selected CPU artifacts and required adapters, without native imports.

    Paths are package-prefix relative, supporting isolated and merged installs.
    Hashes bind the backend, heap hook and Python runtime even when overlay order
    selects dependencies from different prefixes. Generated providers retain their
    existing native ABI/schema handshake when the application creates endpoints.
    """
    manifest = read_json(_prefix('hermos_runtime', environment) / 'share/hermos_runtime/cpu-manifest.json')
    settings = os.environ if environment is None else environment
    expected = dict(manifest_version=1, architecture=platform.machine(),
                    ros_distro=settings.get('ROS_DISTRO'),
                    module='hermos_rclpy', device='/dev/hermos_rclpy')
    for key, value in expected.items():
        if manifest.get(key) != value or value is None:
            raise InstallationError(f'installation {key} mismatch: expected {value!r}')
    if type(manifest.get('adapter_abi')) is not int or not isinstance(manifest.get('build_abi'), str):
        raise InstallationError('missing adapter ABI in CPU manifest')
    entries = manifest.get('artifacts')
    packages = dict(library='hermoslib', heaphook='hermoslib',
                    registry='rclpy_hermos', native='rclpy_hermos')
    if not isinstance(entries, dict) or set(entries) != set(packages):
        raise InstallationError('CPU manifest must identify library, heaphook, registry and native')
    paths = {}
    for role, package in packages.items():
        entry = entries[role]
        if not isinstance(entry, dict) or entry.get('package') != package:
            raise InstallationError(f'invalid artifact package: {role}')
        path = _prefix(package, environment) / relative_path(entry.get('path'))
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise InstallationError(f'missing/unreadable {role} artifact: {path}') from error
        if digest != entry.get('sha256'):
            raise InstallationError(f'artifact digest mismatch: {role}; rebuild the selected overlay')
        paths[role] = path.resolve()
    if paths['heaphook'].name != 'libhermos_heaphook.so' or (
            paths['heaphook'].with_name('libhermos.so').resolve() != paths['library']):
        raise InstallationError('heaphook and library are not matching siblings')
    for provider in providers:
        _verify_provider(provider, manifest, environment)
    return Installation(paths['heaphook'], paths['library'], Path(manifest['device']), paths, manifest)
