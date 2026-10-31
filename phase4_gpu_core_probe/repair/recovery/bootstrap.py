"""Standalone root-owned namespace bootstrap; never import checkout code as root.

Install this single file at the fixed administrator-owned helper path. Invoke it
with the system interpreter's isolated mode. It leaves stdin untouched and execs
the caller's worker only after permanently dropping all root credentials.
"""

import ctypes
import json
import os
from pathlib import Path
import pwd
import sys


def namespaces():
    return {name: os.readlink('/proc/self/ns/' + name) for name in ('pid', 'ipc', 'mnt')}


def syscall(name, *args):
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, name)
    function.restype = ctypes.c_int
    if function(*args) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), name)


def isolate(uid, gid):
    """Derive account permissions and mount size locally, isolate, then drop root."""
    if os.geteuid() != 0 or uid <= 0 or gid < 0:
        raise ValueError('bootstrap requires sudo from a non-root user')
    if Path('/dev/shm').resolve() != Path('/dev/shm'):
        raise RuntimeError('/dev/shm must be a real directory')
    account = pwd.getpwuid(uid)
    groups = sorted(set(os.getgrouplist(account.pw_name, gid)))
    capacity = os.statvfs('/dev/shm')
    size = capacity.f_blocks * capacity.f_frsize
    if size <= 0:
        raise RuntimeError('/dev/shm capacity must be positive')
    before = namespaces()
    previous_device = os.stat('/dev/shm').st_dev
    syscall('unshare', ctypes.c_int(0x08000000 | 0x00020000))  # NEWIPC | NEWNS
    syscall('mount', None, b'/', None, ctypes.c_ulong((1 << 18) | (1 << 14)), None)
    syscall('mount', b'tmpfs', b'/dev/shm', b'tmpfs', ctypes.c_ulong(2 | 4),
            f'mode=1777,size={size}'.encode())  # NOSUID | NODEV
    syscall('prctl', ctypes.c_int(36), ctypes.c_ulong(1), 0, 0, 0)  # CHILD_SUBREAPER
    os.setgroups(groups)
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)
    syscall('prctl', ctypes.c_int(38), ctypes.c_ulong(1), 0, 0, 0)  # NO_NEW_PRIVS
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines()
                  if ':' in line)
    if (os.getresuid() != (uid,) * 3 or os.getresgid() != (gid,) * 3 or
            sorted(os.getgroups()) != groups or
            any(int(status[key], 16) for key in ('CapInh', 'CapPrm', 'CapEff', 'CapAmb')) or
            int(status['NoNewPrivs']) != 1):
        raise RuntimeError('privilege drop is incomplete')
    after = namespaces()
    device = os.stat('/dev/shm').st_dev
    if (after['pid'] != before['pid'] or after['ipc'] == before['ipc'] or
            after['mnt'] == before['mnt'] or device == previous_device or
            any(Path('/dev/shm').iterdir())):
        raise RuntimeError('private namespace or empty tmpfs was not established')
    return {'before': before, 'after': after, 'shm_device': device,
            'previous_shm_device': previous_device, 'shm_limit_bytes': size,
            'uid': uid, 'gid': gid, 'groups': groups, 'capabilities': 0,
            'no_new_privs': True, 'initial_shm': {}}


def main():
    """Only stderr is available for failures; no caller path is opened as root."""
    try:
        if not sys.flags.isolated:
            raise RuntimeError('bootstrap requires python isolated mode (-I)')
        if len(sys.argv) != 3 or not all(Path(value).is_absolute() for value in sys.argv[1:]):
            raise ValueError('bootstrap requires absolute worker and stage paths')
        uid, gid = int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID'])
        isolation = isolate(uid, gid)
        # The only caller-controlled executable is reached after the permanent
        # credential drop. No environment, request JSON, or local import is used
        # in the privileged phase; stdin still holds the owner's first request.
        os.execve('/usr/bin/python3', ['/usr/bin/python3', '-B', '-E', '-s',
                  sys.argv[1], sys.argv[2], json.dumps(isolation)],
                  {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin'})
        raise RuntimeError('exec unexpectedly returned')
    except Exception as error:
        print('namespace bootstrap failed: ' + repr(error), file=sys.stderr)
        return 70


if __name__ == '__main__':
    raise SystemExit(main())
