"""Bind supervisor and launch imports to the selected runtime installation."""

import json
import os
from pathlib import Path
import sys


def validate_installation(environment=None, *, prefix=None):
    """Return the selected prefix only when every loaded runtime module matches.

    With no explicit prefix, use the environment's ament package index. An
    installed entry passes its invoked prefix without resolving the entry
    symlink. Compare individual module files after resolving their symlinks so
    development symlink installs and relocated copied installs both work.
    This check uses only the standard library and imports no peer runtime.
    """
    settings = os.environ if environment is None else environment
    if prefix is None:
        prefix = next((Path(path) for path in settings.get('AMENT_PREFIX_PATH', '').split(os.pathsep)
                       if path and (Path(path) / 'share/ament_index/resource_index/packages' /
                                    'hermos_runtime').is_file()), None)
        if prefix is None:
            raise RuntimeError('missing installed hermos_runtime package')
    prefix = Path(prefix).absolute()
    manifest_path = prefix / 'share/hermos_runtime/supervision.json'
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as error:
        raise RuntimeError('cannot read runtime installation manifest: ' + str(manifest_path)) from error
    value = manifest.get('python_dir') if isinstance(manifest, dict) else None
    relative = Path(value) if isinstance(value, str) else Path('.')
    if (not isinstance(manifest, dict) or type(manifest.get('version')) is not int or
            manifest['version'] != 1 or relative.is_absolute() or
            '..' in relative.parts or relative == Path('.')):
        raise RuntimeError('invalid Python install path in supervision manifest: ' + str(manifest_path))
    python_dir = prefix / relative
    for name, module in tuple(sys.modules.items()):
        if name != 'hermos_runtime' and not name.startswith('hermos_runtime.'):
            continue
        filename = getattr(module, '__file__', None)
        expected = python_dir.joinpath(*name.split('.'))
        expected = (expected / '__init__.py' if hasattr(module, '__path__')
                    else expected.with_suffix('.py'))
        if (filename is None or not expected.is_file() or
                Path(filename).resolve() != expected.resolve()):
            raise RuntimeError(f'mixed hermos_runtime installation for {name}: '
                               f'imported {filename}, selected {expected}')
    return prefix
