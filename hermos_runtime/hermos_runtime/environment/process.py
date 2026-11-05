"""Linux process/device checks before importing application code."""

import ctypes
import importlib.machinery
import os
from pathlib import Path
import stat
import subprocess

from .manifest import InstallationError


class _DlInfo(ctypes.Structure):
    _fields_ = [('filename', ctypes.c_char_p), ('base', ctypes.c_void_p),
                ('symbol', ctypes.c_char_p), ('address', ctypes.c_void_p)]


def malloc_provider():
    """Return the object that supplies this process's active malloc symbol."""
    info = _DlInfo()
    dladdr = ctypes.CDLL('libdl.so.2').dladdr
    dladdr.argtypes = [ctypes.c_void_p, ctypes.POINTER(_DlInfo)]
    dladdr.restype = ctypes.c_int
    if not dladdr(ctypes.cast(ctypes.CDLL(None).malloc, ctypes.c_void_p), ctypes.byref(info)):
        raise InstallationError('cannot identify active malloc provider')
    return Path(os.fsdecode(info.filename)).resolve()


def loaded_paths():
    """Resolve mapped objects, including a deleted image's original pathname."""
    result = set()
    for line in Path('/proc/self/maps').read_text().splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) == 6 and fields[5].startswith('/'):
            result.add(Path(fields[5].removesuffix(' (deleted)')).resolve())
    return result


def check_device(installation):
    """Distinguish absent modules, wrong device nodes and permission failures."""
    module = Path('/sys/module') / installation.manifest['module']
    if not module.is_dir():
        raise InstallationError(f'missing kernel module {module.name}; ask the administrator to load it')
    try:
        info = installation.device.stat()
    except FileNotFoundError as error:
        raise InstallationError(f'missing device: {installation.device}') from error
    if not stat.S_ISCHR(info.st_mode):
        raise InstallationError(f'not a character device: {installation.device}')
    expected = Path('/sys/class') / (module.name + '_class') / module.name / 'dev'
    actual = f'{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}'
    if expected.read_text().strip() != actual:
        raise InstallationError(f'device/module identity mismatch: {installation.device}')
    if not os.access(installation.device, os.R_OK | os.W_OK):
        raise InstallationError(f'permission denied: {installation.device}; check the hermos group/udev policy')


def check_python_origin(installation):
    """Inspect Python search paths without executing rclpy_hermos.__init__."""
    package = importlib.machinery.PathFinder.find_spec('rclpy_hermos')
    if package is None or not package.submodule_search_locations:
        raise InstallationError('missing Python package rclpy_hermos; source the selected overlay')
    native = importlib.machinery.PathFinder.find_spec(
        'rclpy_hermos._native', package.submodule_search_locations)
    if native is None or Path(native.origin).resolve() != installation.artifacts['native']:
        raise InstallationError('Python/native prefix mismatch; source only the selected overlay')


def check_factory_python_origin(installation, executable, environment):
    """Check the selected factory entry's Python environment without loading payloads.

    CPU/GPU factory entries use the repository's fixed system-Python shebang.
    The inspector retains the child's PYTHONPATH/PYTHONHOME/PATH, removes preload,
    and substitutes the resolved script directory for ``-c``'s cwd search entry.
    PathFinder inspects the package and native extension without executing either.
    """
    try:
        entry = Path(executable).resolve(strict=True)
        with entry.open('rb') as stream:
            if stream.readline().rstrip() != b'#!/usr/bin/python3':
                raise InstallationError('factory entry must use #!/usr/bin/python3: ' + str(entry))
    except OSError as error:
        raise InstallationError('cannot inspect factory entry: ' + str(executable)) from error
    code = '''
import sys
sys.path[0] = sys.argv[1]
from importlib.machinery import PathFinder
from pathlib import Path
package = PathFinder.find_spec('rclpy_hermos')
if package is None or not package.submodule_search_locations:
    raise SystemExit('missing Python package rclpy_hermos; source the selected overlay')
native = PathFinder.find_spec('rclpy_hermos._native', package.submodule_search_locations)
if native is None or native.origin is None or Path(native.origin).resolve() != Path(sys.argv[2]):
    raise SystemExit('Python/native prefix mismatch; source only the selected overlay')
'''
    inspector_environment = dict(environment)
    inspector_environment.pop('LD_PRELOAD', None)
    try:
        result = subprocess.run(
            ['/usr/bin/python3', '-c', code, str(entry.parent), str(installation.artifacts['native'])],
            env=inspector_environment, capture_output=True, text=True, timeout=5, check=False)
    except subprocess.TimeoutExpired as error:
        raise InstallationError('factory Python environment check timed out') from error
    except OSError as error:
        raise InstallationError('cannot inspect factory Python environment: ' + str(error)) from error
    if result.returncode:
        raise InstallationError('cannot inspect factory Python environment: ' + result.stderr.strip())
