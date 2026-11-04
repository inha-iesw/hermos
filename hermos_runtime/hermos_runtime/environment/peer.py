"""Prepare only the environment of a peer process."""

import os
from pathlib import Path
import subprocess

from .manifest import InstallationError


def peer_environment(installation, environment=None):
    """Copy caller settings, add the selected allocator and force CPU transport policy.

    Other absolute preload paths are preserved. Allocator conflicts and malformed
    hook selections fail instead of silently replacing caller configuration.
    """
    result = dict(os.environ if environment is None else environment)
    entries = result.get('LD_PRELOAD', '').replace(':', ' ').split()
    hooks = [item for item in entries if Path(item).name == 'libhermos_heaphook.so'
             or (Path(item).is_absolute() and Path(item).resolve() == installation.heaphook)]
    if len(hooks) > 1 or any(not Path(item).is_absolute() or
            Path(item).resolve() != installation.heaphook for item in hooks):
        raise InstallationError('conflicting heaphook preload; use the selected installation')
    for item in entries:
        if any(name in Path(item).name for name in ('jemalloc', 'tcmalloc', 'mimalloc')):
            raise InstallationError(f'conflicting allocator preload: {item}')
        if not Path(item).is_absolute() or not Path(item).is_file():
            raise InstallationError(f'other preload must be an existing absolute path: {item}')
    extra = [item for item in entries if item not in hooks]
    result['LD_PRELOAD'] = ':'.join([str(installation.heaphook), *extra])
    libraries = [str(installation.library.parent)]
    if installation.artifacts:
        libraries.append(str(installation.artifacts['registry'].parent))
    libraries.extend(result.get('LD_LIBRARY_PATH', '').split(os.pathsep))
    result['LD_LIBRARY_PATH'] = os.pathsep.join(dict.fromkeys(path for path in libraries if path))
    result.update(HERMOS_DEVICE_PATH=str(installation.device), HERMOS_BRIDGE_MODE='off',
                  HERMOS_NO_DISCOVERY_AGENT='1')
    return result


def check_extra_preloads(installation, environment):
    """Reject competing exported allocators without loading the user's libraries."""
    for item in environment['LD_PRELOAD'].split(':'):
        if Path(item).resolve() == installation.heaphook:
            continue
        inspector_env = dict(environment)
        inspector_env.pop('LD_PRELOAD', None)
        result = subprocess.run(['readelf', '--dyn-syms', '--wide', item],
                                capture_output=True, text=True, check=False, env=inspector_env)
        if result.returncode:
            raise InstallationError(f'cannot inspect preload library: {item}')
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 8 and fields[6] != 'UND' and fields[7].split('@')[0] in {
                    'malloc', 'calloc', 'realloc', 'free'}:
                raise InstallationError(f'competing allocator in preload library: {item}')
