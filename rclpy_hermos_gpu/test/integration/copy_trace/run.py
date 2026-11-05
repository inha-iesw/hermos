"""Run a diagnostic-only CUDA/NVTX trace inside the verified device session."""

import json
import os
from pathlib import Path
import subprocess
import sys

from hermos_runtime.supervision.isolated import trusted_bootstrap

from analyze import analyze
from support import inventory


def main():
    if not __debug__ or os.environ.get('G4_RUN_DEVICE') != '1':
        raise RuntimeError('requires unoptimized Python and the G4 device session')
    stage = Path(sys.argv[1])
    if not stage.is_absolute():
        raise ValueError('trace stage must be absolute and new')
    bootstrap = trusted_bootstrap()
    stage.mkdir()
    source = Path(__file__).resolve().parent
    environment = dict(os.environ)
    for name in ('LD_PRELOAD', 'G3_RECOVERY_MANIFEST', 'G3_RECOVERY_ACK'):
        environment.pop(name, None)
    nsys = '/usr/local/cuda-13.2/bin/nsys'
    command = [nsys, 'profile', '--trace=cuda,nvtx', '--cuda-trace-all-apis=true',
               '--sample=none', '--cpuctxsw=none', '--wait=all', '--export=sqlite',
               '--output=' + str(stage / 'trace'),
               sys.executable, '-u', str(source / 'pair.py'), str(stage)]
    request = dict(environment=environment, cwd=str(Path.cwd()), command=command)
    (stage / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    before = inventory()
    with (stage / 'nsys.log').open('w') as output:
        result = subprocess.run(
            ['sudo', '-n', '/usr/bin/python3', '-I', '-B', bootstrap,
             str(source / 'namespace_worker.py'), str(stage)],
            input=json.dumps(request), text=True, stdout=output, stderr=subprocess.STDOUT,
            cwd='/', env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin'})
    after = inventory()
    wrapper = dict(exit_code=result.returncode, host_shm_unchanged=before == after)
    (stage / 'wrapper.json').write_text(json.dumps(wrapper, indent=2) + '\n')
    if result.returncode or before != after:
        raise RuntimeError('diagnostic failed; inspect ' + str(stage))
    report = analyze(stage / 'trace.sqlite', stage / 'pair.json')
    (stage / 'analysis.json').write_text(json.dumps(report, indent=2) + '\n')
    if not report['pass']:
        raise RuntimeError('copy-trace oracle failed; inspect analysis.json')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
