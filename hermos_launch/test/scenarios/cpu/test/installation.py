"""Materialize and relocate only selected installed runtime packages."""

import os
from pathlib import Path
import shutil
import tempfile

from ament_index_python.packages import get_package_prefix


PACKAGES = (
    'hermos_cpu_examples', 'hermos_launch', 'hermos_runtime', 'rclpy_hermos_adapters',
    'rclpy_hermos', 'rclpy_hermos_msgs', 'hermoslib',
    'hermos_cie_config_msgs', 'hermos_cie_thread_configurator',
)


class CopiedInstallation:
    """Copy installed files with symlinks dereferenced, then move the whole tree."""

    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hermos-installed-', dir='/tmp')
        root = Path(self.temporary.name)
        staging = root / 'staged'
        prefixes = dict.fromkeys(Path(get_package_prefix(name)) for name in PACKAGES)
        for index, prefix in enumerate(prefixes):
            assert not list(prefix.rglob('*.egg-link')), (
                f'{prefix}: rebuild ament_python packages without --symlink-install before relocation')
            destination = staging / str(index)
            shutil.copytree(prefix, destination, symlinks=False,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            prefixes[prefix] = str(index)
        self.root = root / 'relocated'
        staging.rename(self.root)
        copied = [self.root / index for index in prefixes.values()]
        environment = dict(os.environ)
        for name in tuple(environment):
            if name.startswith(('HERMOS_', '_HERMOS_', 'RCLPY_HERMOS_')) or name in {
                    'LD_PRELOAD', 'PYTHONHOME', 'CMAKE_PREFIX_PATH', 'COLCON_PREFIX_PATH'}:
                environment.pop(name)
        environment['AMENT_PREFIX_PATH'] = os.pathsep.join(map(str, [*copied, Path('/opt/ros/humble')]))
        # Rebase the paths exported by the installed packages' environment hooks.
        # ament_cmake uses local/lib/.../dist-packages on this platform, while
        # ament_python uses lib/.../site-packages; neither layout is assumed.
        python_paths = []
        for part in os.environ.get('PYTHONPATH', '').split(os.pathsep):
            for prefix, index in prefixes.items():
                if Path(part).is_relative_to(prefix):
                    python_paths.append(str(self.root / index / Path(part).relative_to(prefix)))
                    break
            else:
                if part.startswith('/opt/ros/'):
                    python_paths.append(part)
        environment['PYTHONPATH'] = os.pathsep.join(python_paths)
        libraries = [str(prefix / 'lib') for prefix in copied]
        libraries += [part for part in os.environ.get('LD_LIBRARY_PATH', '').split(os.pathsep)
                      if part.startswith('/opt/ros/')]
        environment['LD_LIBRARY_PATH'] = os.pathsep.join(libraries)
        environment['PATH'] = '/opt/ros/humble/bin:/usr/local/bin:/usr/bin:/bin'
        environment['ROS_LOG_DIR'] = str(root / 'ros-log')
        environment['ROS_DOMAIN_ID'] = str(140 + os.getpid() % 50)
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        environment['PWD'] = str(self.root)
        for name in ('AMENT_PREFIX_PATH', 'PYTHONPATH', 'LD_LIBRARY_PATH', 'PATH'):
            assert '/workspace/' not in environment[name], (name, environment[name])
        self.environment = environment

    def close(self):
        self.temporary.cleanup()
