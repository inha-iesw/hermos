"""Host-only privilege-boundary regressions; every side effect is fail-closed."""

import ctypes
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

import importlib.util

SOURCE = Path(__file__).resolve().parents[2] / 'packaging/system/namespace-bootstrap.py'
spec = importlib.util.spec_from_file_location('bootstrap', SOURCE)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


@pytest.fixture(autouse=True)
def block_privileged_or_unowned_operations(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail('unmocked privileged operation or process operation')

    monkeypatch.setattr(bootstrap, 'syscall', blocked)
    monkeypatch.setattr(ctypes, 'CDLL', blocked)
    for name in ('Popen', 'run', 'check_output'):
        monkeypatch.setattr(subprocess, name, blocked)
    for name in ('setgroups', 'setresgid', 'setresuid', 'setgid', 'setuid',
                 'setegid', 'seteuid', 'waitpid', 'pidfd_open', 'kill', 'fork', 'execve'):
        monkeypatch.setattr(os, name, blocked)
    monkeypatch.setattr(signal, 'pidfd_send_signal', blocked)


SETUP = ['unshare', 'private_mount', 'tmpfs', 'subreaper', 'setgroups',
         'setresgid', 'setresuid', 'no_new_privs']


def simulated_bootstrap(monkeypatch, tmp_path, failure=None, postcondition=None):
    steps = []
    state = {'uid': 0, 'gid': 0, 'groups': [], 'unshared': False,
             'mounted': False, 'no_new_privs': False, 'exec': None}
    stage = tmp_path / 'unused-stage'

    def operation(name, update=None):
        steps.append(name)
        if name == failure:
            raise OSError('injected ' + name)
        if update:
            update()

    def syscall(name, *args):
        if name == 'unshare':
            operation('unshare', lambda: state.update(unshared=True))
        elif name == 'mount' and args[1] == b'/':
            operation('private_mount')
        elif name == 'mount' and args[1] == b'/dev/shm':
            assert args[4] == b'mode=1777,size=4096'
            operation('tmpfs', lambda: state.update(mounted=True))
        elif name == 'prctl' and args[0].value == 36:
            operation('subreaper')
        elif name == 'prctl' and args[0].value == 38:
            operation('no_new_privs', lambda: state.update(no_new_privs=True))
        else:
            pytest.fail('unexpected privileged syscall')

    def namespaces():
        changed = state['unshared']
        return {'pid': 'changed' if changed and postcondition == 'pid' else 'same',
                'ipc': 'new' if changed and postcondition != 'ipc' else 'old',
                'mnt': 'new' if changed and postcondition != 'mnt' else 'old'}

    stat, read_text = os.stat, Path.read_text
    def information(path, *args, **kwargs):
        if str(path) == '/dev/shm':
            return SimpleNamespace(st_dev=2 if state['mounted'] and postcondition != 'device' else 1)
        return stat(path, *args, **kwargs)
    def contents(path, *args, **kwargs):
        if str(path) == '/proc/self/status':
            return ''.join(f'{key}:\t{int(postcondition == key)}\n'
                           for key in ('CapInh', 'CapPrm', 'CapEff', 'CapAmb')) + (
                f'NoNewPrivs:\t{int(state["no_new_privs"] and postcondition != "NoNewPrivs")}\n')
        return read_text(path, *args, **kwargs)

    def execve(executable, argv, environment):
        assert state['uid'] == state['gid'] == 1000 and state['no_new_privs']
        state['exec'] = (executable, argv, environment)
        steps.append('exec')
        raise SystemExit(0)  # Successful exec never returns to the privileged helper.

    monkeypatch.setattr(bootstrap, 'syscall', syscall)
    monkeypatch.setattr(bootstrap, 'namespaces', namespaces)
    monkeypatch.setattr(bootstrap.pwd, 'getpwuid', lambda uid: SimpleNamespace(pw_name='caller'))
    def authorized_groups(name, gid):
        assert name == 'caller' and gid == 1000
        return [1000, 44]
    monkeypatch.setattr(os, 'getgrouplist', authorized_groups)
    monkeypatch.setattr(os, 'statvfs', lambda path: SimpleNamespace(f_blocks=1, f_frsize=4096))
    monkeypatch.setattr(os, 'stat', information)
    monkeypatch.setattr(Path, 'read_text', contents)
    monkeypatch.setattr(Path, 'iterdir', lambda path: iter([Path('unexpected')] if postcondition == 'inventory' else []))
    monkeypatch.setattr(os, 'getuid', lambda: state['uid'])
    monkeypatch.setattr(os, 'getgid', lambda: state['gid'])
    monkeypatch.setattr(os, 'geteuid', lambda: state['uid'])
    monkeypatch.setattr(os, 'getresuid', lambda: (state['uid'],) * 3 if postcondition != 'uid' else (0, 1000, 0))
    monkeypatch.setattr(os, 'getresgid', lambda: (state['gid'],) * 3 if postcondition != 'gid' else (0, 1000, 0))
    monkeypatch.setattr(os, 'getgroups', lambda: state['groups'] if postcondition != 'groups' else [0])
    monkeypatch.setattr(os, 'setgroups', lambda groups: operation(
        'setgroups', lambda: state.update(groups=groups)))
    monkeypatch.setattr(os, 'setresgid', lambda real, effective, saved: operation(
        'setresgid', lambda: state.update(gid=real)))
    monkeypatch.setattr(os, 'setresuid', lambda real, effective, saved: operation(
        'setresuid', lambda: state.update(uid=real)))
    monkeypatch.setattr(os, 'execve', execve)
    monkeypatch.setattr(os, 'environ', {'SUDO_UID': '1000', 'SUDO_GID': '1000',
                                      'PYTHONPATH': '/caller', 'LD_PRELOAD': '/caller.so'})
    monkeypatch.setattr(sys, 'argv', ['namespace-bootstrap.py', '/caller/namespace_worker.py', str(stage)])
    monkeypatch.setattr(sys, 'flags', SimpleNamespace(isolated=1))
    # An attempt to obtain groups/capacity from this owner-controlled request is
    # observable: the bootstrap must leave every byte for the unprivileged worker.
    malicious_request = io.StringIO('{"groups": [0, 6, 42], "shm_bytes": 999999999999999}\n')
    monkeypatch.setattr(sys, 'stdin', malicious_request)
    return steps, state, stage, malicious_request


@pytest.mark.parametrize('failure', SETUP)
def test_failed_setup_never_execs_caller_or_writes_stage(monkeypatch, tmp_path, failure):
    steps, state, stage, stream = simulated_bootstrap(monkeypatch, tmp_path, failure=failure)
    assert bootstrap.main() == 70
    assert steps == SETUP[:SETUP.index(failure) + 1]
    assert state['exec'] is None and not stage.exists() and stream.tell() == 0


@pytest.mark.parametrize('postcondition', [
    'CapInh', 'CapPrm', 'CapEff', 'CapAmb', 'NoNewPrivs', 'uid', 'gid', 'groups',
    'pid', 'ipc', 'mnt', 'device', 'inventory',
])
def test_failed_postcondition_never_execs_caller(monkeypatch, tmp_path, postcondition):
    steps, state, stage, stream = simulated_bootstrap(monkeypatch, tmp_path, postcondition=postcondition)
    assert bootstrap.main() == 70
    assert steps == SETUP and state['exec'] is None
    assert not stage.exists() and stream.tell() == 0


def test_bootstrap_derives_authorized_groups_and_capacity_before_nonroot_exec(monkeypatch, tmp_path):
    steps, state, stage, stream = simulated_bootstrap(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as outcome:
        bootstrap.main()
    assert outcome.value.code == 0 and steps == SETUP + ['exec']
    executable, argv, environment = state['exec']
    assert executable == '/usr/bin/python3'
    assert argv[:6] == ['/usr/bin/python3', '-B', '-E', '-s', '/caller/namespace_worker.py', str(stage)]
    isolation = json.loads(argv[6])
    assert isolation['groups'] == [44, 1000] and isolation['shm_limit_bytes'] == 4096
    assert isolation['uid'] == isolation['gid'] == 1000 and isolation['capabilities'] == 0
    assert environment == {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin'}
    assert stream.tell() == 0 and not stage.exists()


@pytest.mark.parametrize('problem', ['nonroot', 'root_caller', 'negative_gid', 'not_isolated',
                                      'relative_worker', 'relative_stage', 'shm_symlink', 'zero_capacity'])
def test_invalid_bootstrap_preconditions_fail_before_side_effects(monkeypatch, tmp_path, problem):
    steps, state, stage, stream = simulated_bootstrap(monkeypatch, tmp_path)
    if problem == 'nonroot':
        state['uid'] = 1000
    elif problem == 'root_caller':
        os.environ['SUDO_UID'] = '0'
    elif problem == 'negative_gid':
        os.environ['SUDO_GID'] = '-1'
    elif problem == 'not_isolated':
        monkeypatch.setattr(sys, 'flags', SimpleNamespace(isolated=0))
    elif problem == 'relative_worker':
        sys.argv[1] = 'relative-worker.py'
    elif problem == 'relative_stage':
        sys.argv[2] = 'relative-stage'
    elif problem == 'shm_symlink':
        monkeypatch.setattr(Path, 'resolve', lambda path: Path('/different/shm'))
    elif problem == 'zero_capacity':
        monkeypatch.setattr(os, 'statvfs', lambda path: SimpleNamespace(f_blocks=0, f_frsize=4096))
    assert bootstrap.main() == 70
    assert not steps and state['exec'] is None and not stage.exists() and stream.tell() == 0
