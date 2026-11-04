"""A bounded exec bootstrap; it owns no worker, signal handler or GPU state."""

import importlib
import os
from pathlib import Path
import sys

from ..environment import InstallationError, peer_environment, resolve_installation
from ..environment.peer import check_extra_preloads
from ..environment.process import check_device, check_python_origin, loaded_paths, malloc_provider


def _prepare(providers):
    installation = resolve_installation(providers=providers)
    environment = peer_environment(installation)
    check_device(installation)
    check_extra_preloads(installation, environment)
    mapped = loaded_paths()
    hooks = {path for path in mapped if path.name == 'libhermos_heaphook.so'}
    if hooks and hooks != {installation.heaphook}:
        raise InstallationError('a different heaphook is already loaded')
    preloaded = installation.heaphook in mapped
    if preloaded and malloc_provider() != installation.heaphook:
        raise InstallationError('selected heaphook does not provide the active malloc')
    libraries = {path for path in mapped if path.name == 'libhermos.so'}
    if libraries and libraries != {installation.library}:
        raise InstallationError('a different libhermos.so is already loaded')
    for role in ('registry', 'native'):
        expected = installation.artifacts[role]
        matches = {path for path in mapped if path.name == expected.name}
        if matches and matches != {expected}:
            raise InstallationError(f'a different {role} artifact is already loaded')
    # The real hook registers this PID before Python main. Exec cannot register
    # it again, and glibc captured its library search path at process startup.
    keys = ('LD_LIBRARY_PATH', 'HERMOS_DEVICE_PATH', 'HERMOS_BRIDGE_MODE',
            'HERMOS_NO_DISCOVERY_AGENT')
    if preloaded and any(os.environ.get(key, '') != environment[key] for key in keys):
        raise InstallationError('already initialized heaphook has a different startup environment; '
                                'start the wrapper without LD_PRELOAD')
    return installation, environment, preloaded


def run(entry_point, *, providers=()):
    """Run a no-argument ``module:function`` after preparing its CPU environment.

    Call from a console script which imports no application modules. The initial
    interpreter execs once, preserving PID, argv and signals. Already prepared
    interpreters do not exec again. The application owns ROS initialization and
    endpoint/alias cleanup, and its return value becomes the console exit status.
    """
    module, separator, function = entry_point.partition(':')
    if not separator or not module or not function.isidentifier():
        raise ValueError('entry_point must be module:function')
    try:
        installation, environment, ready = _prepare(providers)
        check_python_origin(installation)
        if not ready:
            if os.environ.get('_HERMOS_CPU_EXEC') == '1':
                raise InstallationError('heaphook did not load after exec; refusing to retry')
            environment['_HERMOS_CPU_EXEC'] = '1'
            os.execve(sys.executable, [sys.executable, *sys.argv], environment)
        os.environ.update(environment)
        os.environ.pop('_HERMOS_CPU_EXEC', None)
    except (InstallationError, OSError) as error:
        print(f'HERM0S CPU startup: {error}', file=sys.stderr)
        return 78
    return getattr(importlib.import_module(module), function)()


def run_executable(executable, *, providers=()):
    """Exec a native CPU peer with unchanged application arguments and exit status.

    The executable must be an installed absolute path. Unlike Python entry points,
    a native target always needs an exec; the wrapper never imports its runtime.
    """
    try:
        target = Path(executable)
        if not target.is_absolute() or not target.is_file() or not os.access(target, os.X_OK):
            raise InstallationError(f'missing executable: {target}')
        _, environment, preloaded = _prepare(providers)
        if preloaded:
            raise InstallationError('native wrapper must start without the heaphook; '
                                    'exec would register the same PID twice')
        os.execve(str(target), [str(target), *sys.argv[1:]], environment)
    except (InstallationError, OSError) as error:
        print(f'HERM0S CPU startup: {error}', file=sys.stderr)
        return 78
