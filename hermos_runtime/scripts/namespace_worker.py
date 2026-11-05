#!/usr/bin/python3
"""Find this installation only after the administrator bootstrap drops privilege."""

import json
import os
from pathlib import Path
import sys


def main():
    uid, gid = os.getuid(), os.getgid()
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines()
                  if ':' in line)
    if (uid <= 0 or os.getresuid() != (uid,) * 3 or os.getresgid() != (gid,) * 3 or
            any(int(status[key], 16) for key in ('CapInh', 'CapPrm', 'CapEff', 'CapAmb')) or
            int(status['NoNewPrivs']) != 1):
        raise RuntimeError('namespace worker requires permanent privilege drop before import')
    # Do not resolve the entry symlink: ament symlink installs still belong to
    # the invoked prefix, not the development checkout containing this script.
    prefix = Path(__file__).absolute().parents[2]
    manifest = json.loads((prefix / 'share/hermos_runtime/supervision.json').read_text())
    relative = Path(manifest['python_dir'])
    if (manifest.get('version') != 1 or relative.is_absolute() or
            '..' in relative.parts or relative == Path('.')):
        raise RuntimeError('invalid Python install path in supervision manifest')
    python_dir = prefix / relative
    if not (python_dir / 'hermos_runtime/supervision/namespace_worker.py').is_file():
        raise RuntimeError('missing worker at Python install path')
    sys.path.insert(0, str(python_dir))
    from hermos_runtime.supervision.namespace_worker import main as worker_main
    return worker_main()


if __name__ == '__main__':
    raise SystemExit(main())
