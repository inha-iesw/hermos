"""Run payload peers and their supervisor entirely from a relocated installation."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

import pytest
from ament_index_python.packages import get_package_prefix


pytestmark = pytest.mark.skipif(os.environ.get('G4_RUN_DEVICE') != '1',
                               reason='requires verified G4 device session')
PACKAGES = ('hermos_runtime', 'rclpy_hermos', 'rclpy_hermos_msgs', 'rclpy_hermos_adapters', 'hermoslib',
            'hermos_cie_config_msgs', 'hermos_cie_thread_configurator',
            'rclpy_hermos_gpu', 'rclpy_hermos_gpu_msgs', 'ros2_cuda_ipc_msgs')


@pytest.fixture(scope='module')
def installed():
    with tempfile.TemporaryDirectory(prefix='hermos-gpu-install-', dir='/tmp') as temporary:
        root = Path(temporary)
        staged = root / 'staged'
        copied = []
        for name in PACKAGES:
            destination = staged / name
            shutil.copytree(get_package_prefix(name), destination,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            copied.append(root / 'relocated' / name)
        relocated = root / 'relocated'
        staged.rename(relocated)
        apps = relocated / 'apps'
        apps.mkdir()
        tests = Path(__file__).resolve().parents[1]
        for name in ('tensor_peer.py', 'support.py'):
            shutil.copyfile(tests / name, apps / name)
        shutil.copyfile(Path(__file__).with_name('peer_group.py'), apps / 'peer_group.py')
        workspace = Path(__file__).resolve().parents[4]
        shutil.copyfile(workspace / 'build/hermos/repair-probe/phase4_gpu_core_probe/repair/g3_transport_peer',
                        apps / 'cpp_peer')
        (apps / 'cpp_peer').chmod(0o755)
        python_paths = sorted({path.parent for prefix in copied
                               for path in prefix.rglob('__init__.py')
                               if path.parent.name in PACKAGES})
        # Package roots, not individual package directories, belong on sys.path.
        python_paths = sorted({path.parent for path in python_paths})
        system_python = [path for path in os.environ.get('PYTHONPATH', '').split(':')
                         if path.startswith('/opt/ros/')]
        system_libraries = [path for path in os.environ.get('LD_LIBRARY_PATH', '').split(':')
                            if path.startswith('/opt/ros/')]
        env = dict(PATH='/opt/ros/humble/bin:/usr/bin:/bin',
                   PYTHONDONTWRITEBYTECODE='1',
                   PYTHONPATH=':'.join(map(str, [apps, *python_paths, *system_python])),
                   LD_LIBRARY_PATH=':'.join([*(str(path / 'lib') for path in copied), *system_libraries]),
                   AMENT_PREFIX_PATH=':'.join(map(str, [*copied, Path('/opt/ros/humble')])),
                   ROS_DOMAIN_ID=str(140 + os.getpid() % 50), RMW_IMPLEMENTATION='rmw_cyclonedds_cpp',
                   ROS_LOG_DIR=str(root / 'ros-log'))
        assert all('/workspace/' not in value for value in env.values())
        yield apps, env


@pytest.mark.parametrize('producer,consumer', [('python', 'python'), ('python', 'cpp'),
                                             ('cpp', 'python')])
def test_installed_tensor_payload_and_clean_teardown(installed, producer, consumer):
    apps, env = installed
    logs = Path(os.environ['G3_PEER_LOG_ROOT'])
    stage = logs / ('installed-' + uuid.uuid4().hex)
    result = subprocess.run(['/usr/bin/python3', '-B', str(apps / 'peer_group.py'),
                             producer, consumer, str(stage)],
                            env=env, cwd=apps, capture_output=True, text=True, timeout=90)
    (logs / (stage.name + '.stdout')).write_text(result.stdout)
    (logs / (stage.name + '.stderr')).write_text(result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert evidence['result']['verified_teardown'] and evidence['result']['echild']
    assert len(evidence['loaded']) == 4
