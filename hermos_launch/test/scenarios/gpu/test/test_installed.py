"""Opt-in installed ROS launch proof for real CPU/GPU peers and outside DDS."""

import json
import importlib.util
from contextlib import nullcontext
import os
from pathlib import Path
import signal
import subprocess
import uuid

import pytest

from installation import relocated_installation, verify_session_installation


def test_pipeline_package_installs_factory_launch_config_and_dds_entry():
    from ament_index_python.packages import get_package_prefix, get_package_share_directory

    prefix = Path(get_package_prefix('hermos_gpu_examples'))
    share = Path(get_package_share_directory('hermos_gpu_examples'))
    assert (share / 'launch/gpu_pipeline.launch.py').is_file()
    assert (share / 'config/pipeline.yaml').is_file()
    assert os.access(prefix / 'lib/hermos_gpu_examples/dds_control', os.X_OK)
    gpu_prefix = Path(get_package_prefix('rclpy_hermos_gpu'))
    assert os.access(gpu_prefix / 'lib/rclpy_hermos_gpu/hermos_gpu_peer', os.X_OK)
    runtime_prefix = Path(get_package_prefix('hermos_runtime'))
    assert os.access(runtime_prefix / 'lib/hermos_runtime/hermos_metadata_peer', os.X_OK)
    for module in ('peer', 'dds'):
        assert importlib.util.find_spec('hermos_gpu_examples.pipeline.' + module) is not None


def assert_pipeline_proof(directory, relocated_root=None):
    """Check payloads, resolved ROS settings, namespace isolation and final teardown."""
    records = {role: json.loads((directory / f'{role}.json').read_text())
               for role in ('writer', 'reader', 'cpu', 'outside')}
    for role, name, topic in [('writer', 'gpu_writer', '/camera/frames'),
                              ('reader', 'gpu_reader', '/camera/frames'),
                              ('cpu', 'cpu_peer', '/camera/cpu_frames'),
                              ('outside', 'dds_control', '/camera/inside_status')]:
        record = records[role]
        assert record['name'] == name and record['namespace'] == '/camera'
        assert record['topic'] == topic and record['label'] == 'GPU launch 검증'
        if role != 'outside':
            names = {Path(path).name for path in record['mapped_libraries']}
            assert {'libhermos.so', 'libhermos_heaphook.so'} <= names
        if relocated_root is not None:
            for path in [record['factory_module'], *record['mapped_libraries']]:
                assert '/workspace/' not in path and Path(path).is_relative_to(relocated_root), path
    assert len({record['pid'] for record in records.values()}) == 4
    for namespace in ('ipc', 'mnt'):
        assert records['writer'][namespace] == records['reader'][namespace] == records['cpu'][namespace]
        assert records['outside'][namespace] != records['writer'][namespace]
    assert records['writer']['published'] == [3, 17, 91]
    assert records['writer']['acknowledged'] == 3
    assert records['reader']['received'] == [dict(value=value, checksum=64 * value)
                                             for value in (3, 17, 91)]
    assert records['writer']['outside'] == 'outside DDS ready'
    assert records['outside']['received'] == 'inside DDS ready'
    assert records['outside']['preload'] == ''
    from hermos_gpu_examples.pipeline.dds import has_expected_gpu_metadata

    observer = json.loads((directory / 'observer.json').read_text())
    metadata = json.loads((directory / 'metadata.json').read_text())
    assert len({record['pid'] for record in [*records.values(), observer]}) == 5
    assert observer['preload'] == ''
    assert observer['manifest'] == dict(version=1, pid=observer['pid'], resources=[])
    for namespace in ('ipc', 'mnt'):
        assert observer[namespace] == records['writer'][namespace]
    assert not any(name.startswith(('libcuda', 'libtorch', 'libhermos_heaphook')) or name == 'libhermos.so'
                   for name in observer['mapped_names'])
    assert metadata['session_id'] == observer['session_id']
    assert has_expected_gpu_metadata(metadata, writer_pid=records['writer']['pid'],
        reader_pid=records['reader']['pid'], domain_id=observer['domain_id'],
        ipc_ns_inode=int(records['writer']['ipc'].split('[')[1][:-1]))
    bundle = Path(os.environ['RCLPY_HERMOS_KMOD']).resolve().parents[1]
    artifacts = json.loads((bundle / 'artifacts.json').read_text())['artifacts']
    expected = artifacts['lib/libhermos_ioctl_wrapper.so']['sha256']
    assert observer['wrappers'] and set(observer['wrappers'].values()) == {expected}
    if relocated_root is not None:
        entry = next(path for path in observer['argv'] if Path(path).name == 'hermos_metadata_peer')
        for path in [entry, *observer['mapped_libraries']]:
            assert '/workspace/' not in path and Path(path).is_relative_to(relocated_root), path
    stages = list(directory.glob('hermos-group-*/supervisor'))
    assert len(stages) == 1
    cli = json.loads((stages[0] / 'cli.json').read_text())
    assert cli['exit_code'] == 0 and cli['verified_teardown'] and cli['state'] == 'stopped'
    assert cli['restarts'] == 0
    result = json.loads((stages[0] / 'epoch-0/worker-result.json').read_text())
    owner = json.loads((stages[0] / 'epoch-0/owner.json').read_text())
    assert owner['verified_teardown'] and result['teardown_complete'] and result['echild']
    assert not result['peer_failed'] and result['returncodes'] == [0, 0, 0, 0]


@pytest.mark.skipif(os.environ.get('G4_RUN_DEVICE') != '1',
                   reason='requires a verified G4 device session')
@pytest.mark.parametrize('source_free', [False, True], ids=['installed', 'source-free'])
def test_installed_gpu_launch_preserves_payload_and_dds_coexistence(source_free):
    directory = Path(os.environ['G3_PEER_LOG_ROOT']) / ('gpu pipeline ' + uuid.uuid4().hex)
    environment = os.environ.copy()
    environment.pop('LD_PRELOAD', None)
    environment['ROS_DOMAIN_ID'] = str(140 + os.getpid() % 50)
    installation = relocated_installation() if source_free else nullcontext((None, environment))
    with installation as (relocated, environment), \
            directory.with_suffix('.stdout').open('w') as stdout, \
            directory.with_suffix('.stderr').open('w') as stderr:
        verify_session_installation(environment, os.environ['RCLPY_HERMOS_KMOD'])
        process = subprocess.Popen(
            ['ros2', 'launch', 'hermos_gpu_examples', 'gpu_pipeline.launch.py',
             'output_directory:=' + str(directory)], env=environment, cwd=relocated,
            stdout=stdout, stderr=stderr)
        try:
            assert process.wait(timeout=120) == 0, f'inspect {directory}'
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=280)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        assert_pipeline_proof(directory, relocated)
