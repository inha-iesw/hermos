"""One unprivileged GPU peer epoch, execed after the root bootstrap drops privilege."""

import os

if os.geteuid() == 0:
    raise SystemExit('namespace worker must never run as root')

import json
from pathlib import Path
import signal
import sys
import time

from group import Group
from control import JsonInput, write_pipe


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def namespaces():
    return {name: os.readlink('/proc/self/ns/' + name) for name in ('pid', 'ipc', 'mnt')}


def shm_inventory():
    return {p.name: {'bytes': p.stat().st_size, 'inode': p.stat().st_ino}
            for p in Path('/dev/shm').iterdir()}


def process_start_time(pid):
    return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]


def drain_descendants(timeout, record):
    """Reap only after Group has waited all direct peers; never kill a daemon."""
    deadline = time.monotonic() + timeout
    record['descendants'] = []
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            record['echild'] = True
            if any(item['returncode'] != 0 for item in record['descendants']):
                raise RuntimeError('a descendant exited abnormally')
            return
        if pid:
            record['descendants'].append({'pid': pid, 'returncode': os.waitstatus_to_exitcode(status)})
        elif time.monotonic() >= deadline:
            raise TimeoutError('descendant natural-exit deadline expired')
        else:
            time.sleep(0.01)


def run_epoch(request, stage, record, control):
    """Run once; the outer owner alone decides whether to create a new namespace."""
    stopped = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda number, frame: stopped.append(number))
    group = None
    try:
        # Environment is applied only after dropping privilege. It does not load
        # heaphook into this worker; per-peer LD_PRELOAD belongs in commands.env.
        os.environ.clear()
        os.environ.update(request['environment'])
        os.chdir(request['cwd'])
        group = Group(request['commands'], stage / 'group',
                      request['startup_timeout'], request['stop_timeout'])
        group.start()
        save(stage / 'ready.json', {'peers': [p.pid for p in group.processes],
                                   'peer_start_times': {str(p.pid): process_start_time(p.pid)
                                                        for p in group.processes},
                                   'epoch_dir': str(group.epoch_dir)})
        while True:
            exits = [p.poll() for p in group.processes]
            if stopped or any(code is not None for code in exits):
                break
            try:
                command = control.read(0.01)
            except TimeoutError:
                continue
            except EOFError:
                stopped.append(0)  # Parent loss requests complete shutdown.
                break
            if command.get('stop'):
                stopped.append(0)
                break
            peer, data = command['peer'], command['stdin']
            if type(peer) is not int or not 0 <= peer < len(group.processes) or not isinstance(data, str):
                raise ValueError('invalid peer input')
            write_pipe(group.processes[peer].stdin.fileno(), (data + '\n').encode(),
                       request['stop_timeout'])
        failed = any(code is not None and code != 0 for code in exits)
        epoch = group.stop('owner stop' if stopped else 'peer exit')
        failed |= any(p.returncode != 0 and not (
                          p.returncode == -signal.SIGTERM and
                          signal.SIGTERM in epoch['peers'][i].get('signals', []))
                      for i, p in enumerate(group.processes))
        record.update(peer_failed=failed, requested_stop=bool(stopped),
                      cleanup_complete=epoch.get('cleanup') == 'complete',
                      returncodes=[p.returncode for p in group.processes])
    except BaseException as error:
        record['error'] = repr(error)
    finally:
        if group is not None:
            try:
                group.stop('worker final cleanup')
            except BaseException as error:
                record['stop_error'] = repr(error)
        # Do not compete with Popen's wait ownership while any direct peer lives.
        if group is None or all(p.returncode is not None for p in group.processes):
            try:
                drain_descendants(request['stop_timeout'], record)
            except BaseException as error:
                record['drain_error'] = repr(error)
        else:
            record['drain_error'] = 'direct peer death was not confirmed'
        record['final_shm'] = shm_inventory()
        record['teardown_complete'] = bool(
            record.get('echild') and record.get('cleanup_complete') and
            not any(key in record for key in ('error', 'stop_error', 'drain_error')))
        record['retry_allowed'] = bool(record['teardown_complete'] and
                                      record.get('peer_failed') and not record.get('requested_stop'))


def main():
    record = {'version': 1, 'worker_pid': os.getpid(), 'teardown_complete': False,
              'retry_allowed': False}
    verified = False
    try:
        uid, gid = os.getuid(), os.getgid()
        if uid <= 0 or os.getresuid() != (uid,) * 3 or os.getresgid() != (gid,) * 3:
            raise RuntimeError('namespace worker requires permanently dropped credentials')
        isolation = json.loads(sys.argv[2])
        status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines()
                      if ':' in line)
        if (isolation['uid'] != uid or isolation['gid'] != gid or
                sorted(os.getgroups()) != isolation['groups'] or
                any(int(status[key], 16) for key in ('CapInh', 'CapPrm', 'CapEff', 'CapAmb')) or
                int(status['NoNewPrivs']) != 1 or namespaces() != isolation['after'] or
                os.stat('/dev/shm').st_dev != isolation['shm_device'] or shm_inventory()):
            raise RuntimeError('namespace bootstrap handoff is invalid')
        stage = Path(sys.argv[1])
        record['isolation'] = isolation
        verified = True
        control = JsonInput(sys.stdin.fileno())
        request = control.read(10)
        # Handshake lets the unprivileged owner pin the actual worker, not sudo.
        start_time = process_start_time(os.getpid())
        save(stage / 'worker.json', {'pid': os.getpid(), 'start_time': start_time,
                                    'isolation': record['isolation']})
        if control.read(request['startup_timeout']) != {'ack': True}:
            raise RuntimeError('owner did not acknowledge worker identity')
        run_epoch(request, stage, record, control)
    except BaseException as error:
        record['error'] = repr(error)
    finally:
        if verified:
            save(stage / 'worker-result.json', record)
        else:
            print(json.dumps(record), file=sys.stderr)
    return 0 if record['teardown_complete'] else 70


if __name__ == '__main__':
    raise SystemExit(main())
