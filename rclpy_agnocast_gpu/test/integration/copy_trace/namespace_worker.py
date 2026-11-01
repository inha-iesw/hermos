"""Unprivileged diagnostic worker; never imported by the root bootstrap."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time


stage = Path(sys.argv[1])
isolation = json.loads(sys.argv[2])
if os.getuid() == 0 or os.getresuid() != (os.getuid(),) * 3:
    raise RuntimeError('diagnostic worker must run after privilege drop')
request = json.load(sys.stdin)
os.environ.clear()
os.environ.update(request['environment'])
os.chdir(request['cwd'])
result = dict(isolation=isolation, diagnostic_only=True, descendants=[])
result['command_exit_code'] = subprocess.run(request['command']).returncode
deadline = time.monotonic() + 5
while True:
    try:
        pid, status = os.waitpid(-1, os.WNOHANG)
    except ChildProcessError:
        result['echild'] = True
        break
    if pid:
        result['descendants'].append(dict(pid=pid, returncode=os.waitstatus_to_exitcode(status)))
    elif time.monotonic() >= deadline:
        result['drain_timeout'] = True
        break
    else:
        time.sleep(0.01)
result['final_shm'] = {p.name: dict(bytes=p.stat().st_size, inode=p.stat().st_ino)
                       for p in Path('/dev/shm').iterdir()}
(stage / 'diagnostic.json').write_text(json.dumps(result, indent=2) + '\n')
clean = result.get('echild') and all(x['returncode'] == 0 for x in result['descendants'])
raise SystemExit(result['command_exit_code'] if clean else 70)
