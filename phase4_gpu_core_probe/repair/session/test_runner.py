"""Host-only teardown regression; every subprocess and module state is simulated."""

import hashlib
import json
import os
from pathlib import Path
import runpy
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[3]
RUNNER = Path(__file__).with_name('run_device.py')
MODULE = '/sys/module/hermos_rclpy'
HOST_RUN = subprocess.run
HOST_POPEN = subprocess.Popen


def test_optimized_python_is_rejected_before_any_session_side_effect(monkeypatch, tmp_path):
    stage = tmp_path / 'optimized-session'
    flags = {name: getattr(sys.flags, name) for name in dir(sys.flags) if not name.startswith('_')}
    monkeypatch.setattr(sys, 'flags', SimpleNamespace(**(flags | {'optimize': 1})))
    monkeypatch.setattr(sys, 'argv', [str(RUNNER), str(stage), 'unused-command'])
    with pytest.raises(SystemExit, match='requires Python assertions'):
        runpy.run_path(str(RUNNER), run_name='__main__')
    assert not stage.exists()


@pytest.fixture(autouse=True)
def block_unmocked_processes(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail('host regression attempted an unmocked subprocess')

    for name in ('run', 'check_output', 'Popen'):
        monkeypatch.setattr(subprocess, name, blocked)
    for name in ('read_text', 'read_bytes'):
        original = getattr(Path, name)
        def guarded_read(path, *args, _read=original, **kwargs):
            if path.is_relative_to(ROOT / 'build/hermos/device-arm64') or str(path).startswith('/sys/'):
                pytest.fail('host regression attempted to read real device artifacts')
            return _read(path, *args, **kwargs)
        monkeypatch.setattr(Path, name, guarded_read)


def run_simulated(monkeypatch, tmp_path, counts=(0,), test_code=0, removal=0,
                  reuse=False, execute=None, corrupt=False,
                  accessible=True, group_change=True, device_mode='0660',
                  bundle=None, preloaded=False, wrong_device=False,
                  module_name='hermos_rclpy', module_mode='0660', grant_effective=True,
                  host_versions=('preexisting-srcversion', 'preexisting-srcversion')):
    stage = tmp_path / 'session'
    if reuse:
        stage.mkdir()
        (stage / 'device-session.json').write_text('existing evidence\n')
        (stage / 'device-tests.log').write_text('existing test log\n')
    artifact_root = bundle or ROOT / 'build/hermos/device-arm64'
    artifact = bytes(18) + (183).to_bytes(2, 'little')
    source_data = b'simulated source'
    manifest = {
        'kernel': 'simulated-kernel', 'architecture': 'aarch64',
        'vermagic': 'simulated-vermagic', 'srcversion': 'simulated-srcversion',
        'devmode': '0660', 'name': module_name,
        'artifacts': {'kmod/hermos_rclpy.ko': {
            'sha256': hashlib.sha256(artifact).hexdigest(),
            'bytes': len(artifact), 'elf_machine': 183}}}
    source = {'loaded_agnocast_srcversion': 'preexisting-srcversion',
              'simulated-source': hashlib.sha256(source_data).hexdigest()}
    counts = list(counts)
    state = {'loaded': preloaded, 'time': 0.0, 'last_count': None, 'calls': [],
             'accessible': accessible,
             'host_version': host_versions[0]}
    read_text = Path.read_text
    read_bytes = Path.read_bytes
    glob = Path.glob
    exists = Path.exists
    path_stat = Path.stat
    access = os.access

    def simulated_text(path, *args, **kwargs):
        if path == artifact_root / 'artifacts.json':
            return json.dumps(manifest)
        if path == artifact_root / 'source-before.json':
            return json.dumps(source)
        if str(path) == MODULE + '/refcnt':
            count = counts.pop(0) if len(counts) > 1 else counts[0]
            state['last_count'] = count
            return str(count)
        if str(path) == MODULE + '/srcversion':
            return manifest['srcversion']
        if str(path) == '/sys/class/hermos_rclpy_class/hermos_rclpy/dev':
            return '240:0'
        if str(path) == '/sys/class/hermos_rclpy_class/hermos_rclpy/uevent':
            return f'DEVNAME=hermos_rclpy\nDEVMODE={device_mode}\n'
        if str(path) == '/sys/module/agnocast/srcversion':
            if state['host_version'] is None:
                raise FileNotFoundError(str(path))
            return state['host_version']
        return read_text(path, *args, **kwargs)

    def simulated_bytes(path):
        if path == artifact_root / 'kmod/hermos_rclpy.ko':
            return artifact + b'corrupt' if corrupt else artifact
        if path == ROOT / 'simulated-source':
            return source_data
        return read_bytes(path)

    def simulated_glob(path, pattern):
        if str(path) in ('/tmp', '/dev/shm'):
            return iter(())
        return glob(path, pattern)

    def simulated_exists(path):
        if str(path) in (MODULE, '/dev/hermos_rclpy'):
            return state['loaded']
        if str(path) == '/sys/module/agnocast':
            return state['host_version'] is not None
        return exists(path)

    def simulated_run(command, **kwargs):
        # No subprocess can escape this dispatch: an unexpected command fails.
        if command[:3] == ['sudo', '-n', 'insmod']:
            assert command[3:] == [str(artifact_root / 'kmod/hermos_rclpy.ko')]
            assert not state['loaded']
            state['calls'].append('insmod')
            state['loaded'] = True
            return SimpleNamespace(returncode=0)
        if command[:3] == ['sudo', '-n', 'chgrp']:
            assert command == ['sudo', '-n', 'chgrp', str(os.getgid()), '/dev/hermos_rclpy']
            assert state['loaded']
            state['calls'].append('chgrp')
            if not group_change:
                raise subprocess.CalledProcessError(1, command)
            state['accessible'] = grant_effective
            return SimpleNamespace(returncode=0)
        if command == ['sudo', '-n', 'rmmod', 'hermos_rclpy']:
            state['calls'].append('rmmod')
            assert state['last_count'] == 0, 'unload attempted while device is held'
            if removal == 'oserror':
                raise OSError('simulated rmmod launch failure')
            if removal == 0:
                state['loaded'] = False
            if removal and kwargs.get('check'):
                raise subprocess.CalledProcessError(removal, command)
            return SimpleNamespace(returncode=removal)
        assert command == ['mock-test'], command
        state['calls'].append('test')
        state['host_version'] = host_versions[1]
        state['test_env'] = kwargs.get('env', {})
        if execute:
            execute(stage, kwargs)
        return SimpleNamespace(returncode=test_code)

    def simulated_output(command, **kwargs):
        assert command[:2] == ['modinfo', '-F'], command
        assert command[3:] == [str(artifact_root / 'kmod/hermos_rclpy.ko')]
        if command[2] == 'devmode':
            return module_mode + '\n'
        return manifest[command[2]] + '\n'

    def advance(seconds):
        assert seconds > 0
        state['time'] += seconds

    with monkeypatch.context() as patch:
        if bundle is None:
            patch.delenv('RCLPY_HERMOS_DEVICE_BUNDLE', raising=False)
        else:
            patch.setenv('RCLPY_HERMOS_DEVICE_BUNDLE', str(bundle))
        patch.chdir(tmp_path)
        patch.setattr(sys, 'argv', [str(RUNNER), str(stage), 'mock-test'])
        patch.setattr(Path, 'read_text', simulated_text)
        patch.setattr(Path, 'read_bytes', simulated_bytes)
        patch.setattr(Path, 'glob', simulated_glob)
        patch.setattr(os, 'uname', lambda: SimpleNamespace(
            release=manifest['kernel'], machine=manifest['architecture']))
        patch.setattr(Path, 'exists', simulated_exists)
        patch.setattr(Path, 'stat', lambda path, *args, **kwargs: SimpleNamespace(
            st_mode=stat.S_IFCHR | int(device_mode, 8),
            st_rdev=os.makedev(241 if wrong_device else 240, 0), st_gid=os.getgid())
            if str(path) == '/dev/hermos_rclpy' else path_stat(path, *args, **kwargs))
        patch.setattr(os, 'access', lambda path, mode: state['accessible']
                      if str(path) == '/dev/hermos_rclpy' else access(path, mode))
        patch.setattr(subprocess, 'run', simulated_run)
        patch.setattr(subprocess, 'check_output', simulated_output)
        patch.setattr(time, 'monotonic', lambda: state['time'])
        patch.setattr(time, 'sleep', advance)
        try:
            runpy.run_path(str(RUNNER), run_name='__main__')
        except SystemExit as exc:
            outcome = exc.code
        except Exception as exc:
            outcome = exc
        else:
            outcome = 0
    record_path = stage / 'device-session.json'
    text = record_path.read_text() if record_path.exists() else None
    record = json.loads(text) if text and not reuse else None
    return outcome, record, state, stage


def test_inaccessible_owned_device_gets_test_group_before_command(monkeypatch, tmp_path):
    outcome, record, state, _ = run_simulated(monkeypatch, tmp_path, accessible=False)
    assert outcome == 0
    assert state['calls'] == ['insmod', 'chgrp', 'test', 'rmmod']
    assert record['test_device_group'] == os.getgid()
    assert record['devmode'] == '0660'


def test_group_change_failure_stops_command_and_unloads(monkeypatch, tmp_path):
    outcome, record, state, _ = run_simulated(
        monkeypatch, tmp_path, accessible=False, group_change=False)
    assert outcome == 1
    assert state['calls'] == ['insmod', 'chgrp', 'rmmod']
    assert 'test_exit_code' not in record
    assert record['owned_module_unloaded'] is True


def test_successful_chgrp_still_requires_device_access(monkeypatch, tmp_path):
    outcome, record, state, _ = run_simulated(
        monkeypatch, tmp_path, accessible=False, grant_effective=False)
    assert outcome == 1
    assert state['calls'] == ['insmod', 'chgrp', 'rmmod']
    assert 'test_exit_code' not in record


@pytest.mark.parametrize('state', [
    {'module_name': 'other_module'}, {'module_mode': ''}, {'module_mode': '0666'},
])
def test_wrong_or_missing_module_metadata_prevents_loading(monkeypatch, tmp_path, state):
    outcome, record, calls, _ = run_simulated(monkeypatch, tmp_path, **state)
    assert isinstance(outcome, AssertionError)
    assert record is None and calls['calls'] == []


@pytest.mark.parametrize('state', [{'device_mode': '0666'}, {'wrong_device': True}])
def test_wrong_device_identity_prevents_group_change_and_command(monkeypatch, tmp_path, state):
    outcome, record, calls, _ = run_simulated(monkeypatch, tmp_path, accessible=False, **state)
    assert outcome == 1
    assert calls['calls'] == ['insmod', 'rmmod']
    assert 'test_exit_code' not in record


def test_preexisting_device_is_never_changed(monkeypatch, tmp_path):
    outcome, record, state, _ = run_simulated(
        monkeypatch, tmp_path, preloaded=True, accessible=False)
    assert isinstance(outcome, AssertionError)
    assert record is None and state['calls'] == []


def test_explicit_bundle_keeps_hash_checks_and_selects_tld_module(monkeypatch, tmp_path):
    bundle = tmp_path / 'fresh-bundle'
    outcome, record, state, _ = run_simulated(monkeypatch, tmp_path, bundle=bundle)
    assert outcome == 0
    assert record['artifact_directory'] == str(bundle)
    assert state['test_env']['RCLPY_HERMOS_KMOD'] == str(bundle / 'kmod/hermos_rclpy.ko')
    assert record['source_identity_files'] == 1


def test_waits_for_device_holders_before_single_unload(monkeypatch, tmp_path):
    outcome, record, state, _ = run_simulated(monkeypatch, tmp_path, counts=(2, 2, 0))
    assert outcome == 0
    assert state['calls'] == ['insmod', 'test', 'rmmod']
    assert record['refcount_after_tests'] == 2
    assert record['refcount_before_unload'] == 0
    assert 0 < record['refcount_wait_seconds'] <= 5
    assert record['test_exit_code'] == 0
    assert record['owned_module_unloaded'] is True


@pytest.mark.parametrize('test_code', [0, 1])
def test_busy_deadline_is_teardown_failure_and_preserves_test_result(
        monkeypatch, tmp_path, test_code):
    outcome, record, state, _ = run_simulated(
        monkeypatch, tmp_path, counts=(2,), test_code=test_code)
    assert outcome == 70
    assert state['calls'] == ['insmod', 'test']
    assert record['test_exit_code'] == test_code
    assert record['refcount_after_tests'] == record['refcount_before_unload'] == 2
    assert 5 <= record['refcount_wait_seconds'] <= 5.1
    assert record['owned_module_unloaded'] is False


def test_test_failure_keeps_its_exit_code_after_clean_teardown(monkeypatch, tmp_path):
    outcome, record, state, _ = run_simulated(monkeypatch, tmp_path, test_code=1)
    assert outcome == 1
    assert state['calls'] == ['insmod', 'test', 'rmmod']
    assert record['test_exit_code'] == 1
    assert record['owned_module_unloaded'] is True


@pytest.mark.parametrize('removal', [1, 'oserror'])
def test_unload_failure_still_writes_session_json(monkeypatch, tmp_path, removal, capsys):
    outcome, record, state, _ = run_simulated(monkeypatch, tmp_path, removal=removal)
    assert outcome == 70
    assert state['calls'] == ['insmod', 'test', 'rmmod']
    assert record['test_exit_code'] == 0
    assert record['owned_module_unloaded'] is False
    assert record['preexisting_sockets_preserved'] is True
    assert record['metadata_preserved'] is True
    assert record['preexisting_module_preserved'] is True
    summary = json.loads(capsys.readouterr().out)
    assert summary['runner_exit_code'] == 70
    assert summary['test_exit_code'] == 0
    if removal == 'oserror':
        assert 'simulated rmmod launch failure' in record['rmmod_error']
    else:
        assert record['rmmod_exit_code'] == 1


def test_existing_stage_is_rejected_without_overwriting_evidence(monkeypatch, tmp_path):
    outcome, _, state, stage = run_simulated(monkeypatch, tmp_path, reuse=True)
    assert isinstance(outcome, FileExistsError)
    assert state['calls'] == []
    assert (stage / 'device-session.json').read_text() == 'existing evidence\n'
    assert (stage / 'device-tests.log').read_text() == 'existing test log\n'


@pytest.mark.parametrize('arguments', [[], ['relative', 'mock-test'], [''], ['/unused', '']])
def test_invalid_arguments_are_rejected_before_creating_stage(monkeypatch, arguments):
    def unexpected_mkdir(*args, **kwargs):
        pytest.fail('invalid arguments reached stage creation')

    monkeypatch.setattr(sys, 'argv', [str(RUNNER), *arguments])
    monkeypatch.setattr(Path, 'mkdir', unexpected_mkdir)
    with pytest.raises(ValueError, match='absolute stage.*command'):
        runpy.run_path(str(RUNNER), run_name='__main__')


def test_missing_command_is_rejected_without_creating_stage(monkeypatch, tmp_path):
    stage = tmp_path / 'unused'
    monkeypatch.setattr(sys, 'argv', [str(RUNNER), str(stage)])
    with pytest.raises(ValueError, match='absolute stage.*command'):
        runpy.run_path(str(RUNNER), run_name='__main__')
    assert not stage.exists()


@pytest.mark.parametrize('selected_bundle', [False, True])
def test_artifact_mismatch_prevents_module_loading(monkeypatch, tmp_path, selected_bundle):
    outcome, record, state, _ = run_simulated(
        monkeypatch, tmp_path, corrupt=True,
        bundle=tmp_path / 'fresh-bundle' if selected_bundle else None)
    assert isinstance(outcome, AssertionError)
    assert record is None and state['calls'] == []


def test_absent_preexisting_module_stays_absent(monkeypatch, tmp_path):
    outcome, record, state, _ = run_simulated(
        monkeypatch, tmp_path, host_versions=(None, None))
    assert outcome == 0
    assert state['calls'] == ['insmod', 'test', 'rmmod']
    assert record['agnocast_srcversion'] is None
    assert record['agnocast_srcversion_after'] is None
    assert record['preexisting_module_preserved'] is True


@pytest.mark.parametrize('before,after', [
    (None, 'preexisting-srcversion'),
    ('preexisting-srcversion', None),
    ('preexisting-srcversion', 'replacement-srcversion'),
])
def test_preexisting_module_change_fails_teardown(monkeypatch, tmp_path, before, after):
    outcome, record, state, _ = run_simulated(
        monkeypatch, tmp_path, host_versions=(before, after))
    assert outcome == 70
    assert state['calls'] == ['insmod', 'test', 'rmmod']
    assert record['test_exit_code'] == 0
    assert record['owned_module_unloaded'] is True
    assert record['agnocast_srcversion'] == before
    assert record['agnocast_srcversion_after'] == after
    assert record['preexisting_module_preserved'] is False


def test_preexisting_module_mismatch_prevents_loading_owned_module(monkeypatch, tmp_path):
    outcome, record, state, _ = run_simulated(
        monkeypatch, tmp_path, host_versions=('unexpected-srcversion', 'unexpected-srcversion'))
    assert isinstance(outcome, AssertionError)
    assert record is None and state['calls'] == []


def test_session_environment_isolated_without_global_pytest_basetemp(monkeypatch, tmp_path):
    monkeypatch.setenv('SESSION_INHERITED', 'retained')
    outcome, record, state, stage = run_simulated(monkeypatch, tmp_path)
    assert outcome == 0
    assert state['test_env']['TMPDIR'] == str(stage / 'tmp')
    assert state['test_env']['G3_PEER_LOG_ROOT'] == str(stage / 'peers')
    assert state['test_env']['SESSION_INHERITED'] == 'retained'
    assert record['temporary_directory'] == str(stage / 'tmp')
    assert record['peer_log_directory'] == str(stage / 'peers')
    assert (stage / 'tmp').is_dir() and (stage / 'peers').is_dir()


def test_later_pytest_sessions_do_not_remove_peer_logs(monkeypatch, tmp_path):
    # Exercise the real transport fixture's directory selection without CUDA.
    source = ROOT / 'phase4_gpu_core_probe/repair/test/test_transport.py'
    test = tmp_path / 'test_peer_evidence.py'
    test.write_text('''import importlib.util
import subprocess
from pathlib import Path

def test_peer_log(tmp_path, monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('unmocked child subprocess')
    for name in ('run', 'check_output', 'Popen'):
        monkeypatch.setattr(subprocess, name, blocked)
    spec = importlib.util.spec_from_file_location('transport', SOURCE)
    transport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transport)
    class Peer:
        def __init__(self, binary, role, topic, limit, directory, fault=None, **kwargs):
            (directory / (role + '.stdout')).write_text('retained peer output')
        def ask(self, command):
            return ['CONNECTIONS', '1']
        def stop(self):
            pass
    monkeypatch.setattr(transport, 'Peer', Peer)
    monkeypatch.setenv('G3_TRANSPORT_PEER', 'unused')
    fixture = transport.peers.__wrapped__(tmp_path)
    next(fixture)()
    try:
        next(fixture)
    except StopIteration:
        pass
'''.replace('SOURCE', repr(str(source))))
    def execute(stage, kwargs):
        env = kwargs['env'].copy()
        env.pop('PYTEST_ADDOPTS', None)
        env.pop('PYTEST_DEBUG_TEMPROOT', None)
        env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        command = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', str(test)]
        def host_pytest_only(args, **options):
            assert args == command and not options.get('shell')
            return HOST_POPEN(args, **options)

        with monkeypatch.context() as patch:
            patch.setattr(subprocess, 'Popen', host_pytest_only)
            for index in range(5):
                result = HOST_RUN(command, env=env, cwd=tmp_path,
                                  capture_output=True, text=True)
                assert result.returncode == 0, result.stdout + result.stderr
                assert len(list((stage / 'peers').glob('*/*/publisher.stdout'))) == index + 1
        assert all(path.read_text() == 'retained peer output'
                   for path in (stage / 'peers').glob('*/*/*.stdout'))

    outcome, record, _, _ = run_simulated(monkeypatch, tmp_path, execute=execute)
    assert outcome == 0, record
