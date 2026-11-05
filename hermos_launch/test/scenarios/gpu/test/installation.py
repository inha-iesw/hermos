"""Materialize selected CPU/GPU launch packages without source-tree search paths."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from ament_index_python.packages import get_package_prefix
from hermos_runtime.environment import resolve_installation


PACKAGES = (
    'hermos_runtime', 'rclpy_hermos', 'rclpy_hermos_msgs', 'rclpy_hermos_adapters', 'hermoslib',
    'hermos_cie_config_msgs', 'hermos_cie_thread_configurator', 'rclpy_hermos_gpu',
    'rclpy_hermos_gpu_msgs', 'ros2_cuda_ipc_msgs', 'hermos_launch', 'hermos_gpu_examples',
)


def verify_session_installation(environment, module):
    """Bind the selected installation to the device runner's verified libraries."""
    # The runner exports an absolute module path even for a relative/default
    # bundle. Keep this path in the parent; relocated peers need only installed code.
    bundle = Path(module).resolve().parents[1]
    artifacts = json.loads((bundle / 'artifacts.json').read_text())['artifacts']
    selected = resolve_installation(
        providers=['rclpy_hermos_adapters'], environment=environment)
    for path in (selected.heaphook, selected.library):
        relative = 'lib/' + path.name
        assert relative in artifacts, f'bundle lacks {relative}'
        expected = artifacts[relative]['sha256']
        assert hashlib.sha256((bundle / relative).read_bytes()).hexdigest() == expected, (
            f'bundle artifact changed: {relative}')
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, (
            f'installed artifact differs from device bundle: {path}')


@contextmanager
def relocated_installation():
    """Yield copied prefix roots and an environment restricted to installed code."""
    with tempfile.TemporaryDirectory(prefix='hermos-gpu-launch-', dir='/tmp') as temporary:
        root = Path(temporary)
        staged = root / 'staged'
        prefixes = dict.fromkeys(Path(get_package_prefix(name)) for name in PACKAGES)
        for index, prefix in enumerate(prefixes):
            assert not list(prefix.rglob('*.egg-link')), (
                f'{prefix}: rebuild ament_python packages without --symlink-install before relocation')
            destination = staged / str(index)
            shutil.copytree(prefix, destination, symlinks=False,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            prefixes[prefix] = str(index)
        relocated = root / 'relocated'
        staged.rename(relocated)
        copied = [relocated / index for index in prefixes.values()]
        # Derive both ament_cmake dist-packages and ament_python site-packages
        # locations from installed files, never a build-tree environment hook.
        python_paths = sorted({path.parent.parent for prefix in copied
                               for path in prefix.rglob('__init__.py')
                               if path.parent.name in PACKAGES})
        system_python = [part for part in os.environ.get('PYTHONPATH', '').split(os.pathsep)
                         if part.startswith('/opt/ros/')]
        system_libraries = [part for part in os.environ.get('LD_LIBRARY_PATH', '').split(os.pathsep)
                            if part.startswith('/opt/ros/')]
        environment = dict(
            PATH='/opt/ros/humble/bin:/usr/local/bin:/usr/bin:/bin',
            PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1',
            PYTHONPATH=os.pathsep.join(map(str, [*python_paths, *system_python])),
            LD_LIBRARY_PATH=os.pathsep.join([*(str(prefix / 'lib') for prefix in copied),
                                            *system_libraries]),
            AMENT_PREFIX_PATH=os.pathsep.join(map(str, [*copied, Path('/opt/ros/humble')])),
            ROS_DISTRO='humble', ROS_DOMAIN_ID=str(140 + os.getpid() % 50),
            ROS_LOG_DIR=str(root / 'ros-log'),
            PWD=str(relocated))
        assert all('/workspace/' not in value for value in environment.values()), environment
        yield relocated, environment
