# No session JSON means preflight failed; an existing stage is never overwritten.
# Missing test_exit_code means the test command did not complete.
# Exit 70 means teardown failed; otherwise preserve the test's exit code.
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

if sys.flags.optimize:
    raise SystemExit('device runner requires Python assertions; remove -O/PYTHONOPTIMIZE')


def preexisting_module_version():
    """Snapshot the unrelated module, preserving absence as a valid baseline."""
    module = Path('/sys/module/agnocast')
    return (module / 'srcversion').read_text().strip() if module.exists() else None


if len(sys.argv) < 3 or not Path(sys.argv[1]).is_absolute() or not sys.argv[2]:
    raise ValueError('expected an absolute stage path followed by a nonempty command')
root = Path(__file__).resolve().parents[3]
os.chdir(root)
stage = Path(sys.argv[1])
stage.mkdir(exist_ok=False)
command = sys.argv[2:]
temporary = stage / 'tmp'
peer_logs = stage / 'peers'
temporary.mkdir()
peer_logs.mkdir()
environment = os.environ.copy()
environment.update(TMPDIR=str(temporary), G3_PEER_LOG_ROOT=str(peer_logs))
artifact_root = root / 'build/hermos/device-arm64'
manifest = json.loads((artifact_root / 'artifacts.json').read_text())
record = {'command': command, 'verified_artifacts': {},
          'temporary_directory': str(temporary), 'peer_log_directory': str(peer_logs)}
for name, entry in manifest['artifacts'].items():
    data = (artifact_root / name).read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    assert actual == entry['sha256'] and len(data) == entry['bytes'], name
    assert int.from_bytes(data[18:20], 'little') == entry['elf_machine'], name
    record['verified_artifacts'][name] = actual
sources = json.loads((artifact_root / 'source-before.json').read_text())
record['agnocast_srcversion'] = preexisting_module_version()
for name, expected in sources.items():
    if name == 'loaded_agnocast_srcversion':
        if record['agnocast_srcversion'] is not None:
            assert record['agnocast_srcversion'] == expected
    else:
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, name
record['source_identity_files'] = sum(name != 'loaded_agnocast_srcversion' for name in sources)
module = artifact_root / 'kmod/hermos_rclpy.ko'
record['kernel'] = os.uname().release
for key in ('vermagic', 'srcversion'):
    record[key] = subprocess.check_output(['modinfo', '-F', key, str(module)], text=True).strip()
    assert record[key] == manifest[key]
assert record['kernel'] == manifest['kernel']
assert os.uname().machine == manifest['architecture']
assert not Path('/sys/module/hermos_rclpy').exists()
assert not Path('/dev/hermos_rclpy').exists()
record['sockets_before'] = sorted(str(p) for p in Path('/tmp').glob('cuda_memory_pool_*.sock'))
record['metadata_before'] = sorted(str(p) for p in Path('/dev/shm').glob('rclpy_zc_gpu_v1_*'))
cuda_shm = list(Path('/dev/shm').glob('cuda.shm.*'))
record['cuda_shm_before'] = {'files': len(cuda_shm), 'bytes': sum(p.stat().st_size for p in cuda_shm)}
loaded = False
try:
    subprocess.run(['sudo', '-n', 'insmod', str(module)], check=True)
    loaded = True
    deadline = time.monotonic() + 3
    while not Path('/dev/hermos_rclpy').exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert Path('/sys/module/hermos_rclpy/srcversion').read_text().strip() == record['srcversion']
    with (stage / 'device-tests.log').open('w') as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, env=environment)
    record['test_exit_code'] = result.returncode
except Exception as error:
    record['session_error'] = repr(error)
finally:
    try:
        if loaded:
            refcount_path = Path('/sys/module/hermos_rclpy/refcnt')
            started = time.monotonic()
            record['refcount_after_tests'] = refcount = int(refcount_path.read_text())
            # The inherited device FD can outlive the peers in the unlink daemon,
            # which polls every second. Never force-unload or use rmmod to poll.
            deadline = started + 5
            while refcount != 0 and time.monotonic() < deadline:
                time.sleep(0.05)
                refcount = int(refcount_path.read_text())
            record['refcount_wait_seconds'] = time.monotonic() - started
            record['refcount_before_unload'] = refcount
            if refcount == 0:
                try:
                    record['rmmod_exit_code'] = subprocess.run(
                        ['sudo', '-n', 'rmmod', 'hermos_rclpy']).returncode
                except OSError as error:
                    record['rmmod_error'] = repr(error)
        record['owned_module_unloaded'] = not Path('/sys/module/hermos_rclpy').exists()
        record['sockets_after'] = sorted(str(p) for p in Path('/tmp').glob('cuda_memory_pool_*.sock'))
        record['metadata_after'] = sorted(str(p) for p in Path('/dev/shm').glob('rclpy_zc_gpu_v1_*'))
        record['preexisting_sockets_preserved'] = record['sockets_after'] == record['sockets_before']
        record['metadata_preserved'] = record['metadata_after'] == record['metadata_before']
        record['agnocast_srcversion_after'] = preexisting_module_version()
        record['preexisting_module_preserved'] = (
            record['agnocast_srcversion_after'] == record['agnocast_srcversion'])
        cuda_shm = list(Path('/dev/shm').glob('cuda.shm.*'))
        record['cuda_shm_after'] = {'files': len(cuda_shm), 'bytes': sum(p.stat().st_size for p in cuda_shm)}
    except Exception as error:
        record['teardown_error'] = repr(error)
    teardown_ok = (all(record.get(key) for key in (
        'owned_module_unloaded', 'preexisting_sockets_preserved',
        'metadata_preserved', 'preexisting_module_preserved')) and
        record.get('rmmod_exit_code', 0) == 0 and
        'rmmod_error' not in record and 'teardown_error' not in record)
    record['runner_exit_code'] = record.get('test_exit_code', 1) if teardown_ok else 70
    (stage / 'device-session.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps({k: v for k, v in record.items()
                      if k.endswith(('code', 'preserved', 'unloaded', 'error'))}))
sys.exit(record['runner_exit_code'])
