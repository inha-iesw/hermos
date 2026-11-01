"""Exercise actual DDS data and parameter traffic alongside private GPU epochs."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

from support import launcher_argv, successful


pytestmark = pytest.mark.skipif(
    os.environ.get('G4_RUN_DEVICE') != '1', reason='requires the verified G4 device session')
RMWS = ('rmw_cyclonedds_cpp', 'rmw_fastrtps_cpp')


def environment(rmw):
    # Start with the installed RMW's default transports. Cross-mount Fast DDS
    # explicitly selects its measured UDP-only contract below; there is no retry.
    for name in ('CYCLONEDDS_URI', 'FASTDDS_DEFAULT_PROFILES_FILE',
                 'FASTRTPS_DEFAULT_PROFILES_FILE', 'RMW_FASTRTPS_USE_QOS_FROM_XML'):
        assert not os.environ.get(name), f'DDS default-profile test inherited {name}'
    return {
        'RMW_IMPLEMENTATION': rmw,
        'ROS_DOMAIN_ID': str(20 + uuid.uuid4().int % 180),
        'AGNOCAST_BRIDGE_MODE': 'off',
        'AGNOCAST_NO_DISCOVERY_AGENT': '1',
        'LD_PRELOAD': '',
        'PYTHONPATH': str(Path(__file__).parent) + os.pathsep + os.environ.get('PYTHONPATH', ''),
        'PYTHONDONTWRITEBYTECODE': '1',
    }


def command(role, topic, result_path, env):
    return {'argv': launcher_argv('dds_peer:factory', role, topic, result_path,
                                  block_bytes=256, max_message_bytes=256),
            'env': env.copy()}


def results(paths, epoch, external=None):
    deadline = time.monotonic() + 20
    while not all(path.exists() for path in paths):
        if epoch.poll() is not None:
            pytest.fail(f'DDS epoch exited before proof; inspect {epoch.epoch_dir}')
        if external is not None and external.poll() is not None:
            pytest.fail(f'external DDS peer exited {external.returncode} before proof')
        if time.monotonic() >= deadline:
            missing = [str(path) for path in paths if not path.exists()]
            pytest.fail(f'DDS topic/service deadline expired: {missing}; inspect {epoch.epoch_dir}')
        time.sleep(0.01)
    return [json.loads(path.read_text()) for path in paths]


def assert_proof(proofs, topic, rmw):
    assert [proof['role'] for proof in proofs] == ['a', 'b']
    assert proofs[0]['pid'] != proofs[1]['pid']
    for role, proof in zip(('b', 'a'), proofs):
        assert proof['rmw'] == rmw
        assert proof['received'] == f'{topic}:{role}:payload'
        assert proof['parameter'] == f'{topic}:{role}:parameter'


@pytest.mark.parametrize('rmw', RMWS)
def test_dds_topics_and_parameters_within_private_gpu_epoch(epochs, tmp_path, rmw):
    env = environment(rmw)
    topic = '/g4_dds_' + uuid.uuid4().hex
    paths = [tmp_path / f'{role}.json' for role in ('a', 'b')]
    epoch = epochs([command(role, topic, path, env)
                    for role, path in zip(('a', 'b'), paths)])
    proofs = results(paths, epoch)
    assert_proof(proofs, topic, rmw)
    assert proofs[0]['ipc'] == proofs[1]['ipc'] != os.readlink('/proc/self/ns/ipc')
    assert proofs[0]['mnt'] == proofs[1]['mnt'] != os.readlink('/proc/self/ns/mnt')
    assert proofs[0]['shm_device'] == proofs[1]['shm_device'] != os.stat('/dev/shm').st_dev
    successful(epoch.stop())


@pytest.mark.parametrize('rmw,profile_name', [
    pytest.param('rmw_cyclonedds_cpp', None, id='cyclone-default'),
    pytest.param('rmw_fastrtps_cpp', 'fastdds_udp.xml', id='fastdds-udp-only'),
])
def test_dds_topics_and_parameters_cross_private_gpu_epoch(
        epochs, tmp_path, rmw, profile_name):
    env = environment(rmw)
    profile_evidence = {'rmw': rmw, 'profile': 'default', 'path': None, 'sha256': None}
    if profile_name is not None:
        from ament_index_python.packages import get_package_share_directory

        profile = Path(get_package_share_directory('rclpy_agnocast_gpu')) / 'config/dds' / profile_name
        source_profile = Path(__file__).resolve().parents[2] / 'config/dds' / profile_name
        contents = profile.read_bytes()
        assert contents == source_profile.read_bytes(), 'installed DDS profile differs from source'
        env['FASTRTPS_DEFAULT_PROFILES_FILE'] = str(profile)
        profile_evidence.update(profile=profile_name, path=str(profile),
                                sha256=hashlib.sha256(contents).hexdigest())
    (tmp_path / 'transport-profile.json').write_text(json.dumps(profile_evidence, indent=2) + '\n')
    topic = '/g4_dds_' + uuid.uuid4().hex
    paths = [tmp_path / f'{role}.json' for role in ('a', 'b')]
    outside_env = {**os.environ, **env}
    outside_env.pop('LD_PRELOAD', None)
    for name in tuple(outside_env):
        if name.startswith('G3_RECOVERY_') or name.startswith('AGNOCAST_'):
            outside_env.pop(name)
    with (tmp_path / 'outside.stdout').open('w') as stdout, \
            (tmp_path / 'outside.stderr').open('w') as stderr:
        external = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name('dds_peer.py')),
             '--outside', 'b', topic, str(paths[1])],
            env=outside_env, stdout=stdout, stderr=stderr)
        try:
            epoch = epochs([command('a', topic, paths[0], env)])
            proofs = results(paths, epoch, external)
            assert_proof(proofs, topic, rmw)
            assert proofs[0]['ipc'] != proofs[1]['ipc'] == os.readlink('/proc/self/ns/ipc')
            assert proofs[0]['mnt'] != proofs[1]['mnt'] == os.readlink('/proc/self/ns/mnt')
            assert proofs[0]['shm_device'] != proofs[1]['shm_device'] == os.stat('/dev/shm').st_dev
            successful(epoch.stop())
        finally:
            if external.poll() is None:
                external.terminate()
            try:
                code = external.wait(timeout=5)
            except subprocess.TimeoutExpired:
                external.kill()
                external.wait(timeout=5)
                pytest.fail('external DDS peer required SIGKILL')
        assert code == 0
