"""Real Python/C++ registration from a relocated installation, without sources."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.fixture
def relocated(tmp_path):
    original = os.environ.get('HERMOS_RUNTIME_TEST_PREFIX')
    peer = os.environ.get('HERMOS_REGISTRATION_PEER')
    if not original or not peer:
        pytest.skip('run through the hermos_runtime CMake test target')
    original = Path(original)
    prefix = tmp_path / 'relocated'
    share = prefix / 'share/hermos_runtime'
    shutil.copytree(original / 'share/hermos_runtime', share)
    python_dir = json.loads((share / 'supervision.json').read_text())['python_dir']
    shutil.copytree(original / python_dir / 'hermos_runtime',
                    prefix / python_dir / 'hermos_runtime',
                    ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(original / 'lib/hermos_runtime', prefix / 'lib/hermos_runtime')
    marker = prefix / 'share/ament_index/resource_index/packages/hermos_runtime'
    marker.parent.mkdir(parents=True)
    marker.touch()
    cpp = prefix / 'registration_peer'
    shutil.copyfile(peer, cpp)
    cpp.chmod(0o755)
    python = prefix / 'peer.py'
    python.write_text(
        'from hermos_runtime.supervision.registration import Registration\n'
        'Registration.from_environment(required=True).wait([])\n'
        'print("registered", flush=True)\n'
        'assert input() == "finish"\n')
    system_python = [path for path in os.environ.get('PYTHONPATH', '').split(':')
                     if path.startswith('/opt/ros/')]
    env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
           'PYTHONDONTWRITEBYTECODE': '1',
           'PYTHONPATH': ':'.join([str(prefix / python_dir), *system_python]),
           'AMENT_PREFIX_PATH': str(prefix) + ':/opt/ros/humble',
           'HERMOS_BRIDGE_MODE': 'off', 'HERMOS_NO_DISCOVERY_AGENT': '1'}
    commands = [{'argv': ['/usr/bin/python3', str(python)]}, {'argv': [str(cpp)]}]
    return prefix, env, commands


@pytest.mark.parametrize('name', ['hermos_supervisor', 'hermos_cpu_peer'])
def test_installed_cli_is_directly_executable(relocated, name):
    prefix, env, _ = relocated
    result = subprocess.run([str(prefix / 'lib/hermos_runtime' / name), '--help'],
                            env=env, cwd=prefix, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'usage:' in result.stdout


def test_source_free_python_cpp_registration_and_native_free_import(relocated):
    prefix, env, commands = relocated
    code = '''import json, sys, time
from pathlib import Path
from hermos_runtime.supervision.group import Group
from hermos_runtime.supervision import isolated, namespace_worker, run
assert not any(name.split('.')[0] in {'rclpy_hermos', 'rclpy_hermos_gpu', 'torch'}
               for name in sys.modules)
group = Group(json.loads(sys.argv[1]), Path(sys.argv[2]), stop_timeout=1)
try:
    group.start()
    for peer in group.processes:
        peer.stdin.write('finish\\n')
        peer.stdin.flush()
    deadline = time.monotonic() + 5
    for peer in group.processes:
        while Path(f'/proc/{peer.pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z':
            assert time.monotonic() < deadline
            time.sleep(.005)
finally:
    result = group.stop('test complete')
assert result['cleanup'] == 'complete'
assert all(peer.returncode == 0 for peer in group.processes)
print(json.dumps(result))
'''
    result = subprocess.run(['/usr/bin/python3', '-B', '-c', code,
                             json.dumps(commands), str(prefix / 'group')],
                            env=env, cwd=prefix, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads(result.stdout)
    assert record['registered'] and len(record['peers']) == 2
    for index in range(2):
        assert (prefix / f'group/epoch-0/peer-{index}.stdout').read_text().strip() == 'registered'


@pytest.mark.skipif(os.environ.get('HERMOS_TEST_NAMESPACE') != '1',
                    reason='requires administrator-installed namespace bootstrap')
def test_source_free_installed_worker_with_python_cpp_peers(relocated):
    prefix, env, commands = relocated
    code = '''import json, sys
from pathlib import Path
from hermos_runtime.supervision.isolated import IsolatedEpoch
epoch = IsolatedEpoch(json.loads(sys.argv[1]), Path(sys.argv[2]), stop_timeout=1)
try:
    epoch.start()
    maps = Path(f'/proc/{epoch.worker_pid}/maps').read_text()
    assert not any(name in maps for name in ('libcuda', 'libhermos', 'libtorch'))
    for index in range(len(epoch.peers)):
        epoch.send(index, 'finish')
    result = epoch.wait(timeout=15)
finally:
    epoch.stop()
assert result['verified_teardown'] and not result['peer_failed']
print(json.dumps(result))
'''
    result = subprocess.run(['/usr/bin/python3', '-B', '-c', code,
                             json.dumps(commands), str(prefix / 'epoch')],
                            env=env, cwd=prefix, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads(result.stdout)
    assert record['echild'] and record['teardown_complete']
    assert record['isolation']['before']['ipc'] != record['isolation']['after']['ipc']
    assert record['isolation']['before']['mnt'] != record['isolation']['after']['mnt']
