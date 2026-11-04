"""Installed examples expose only opted-in wrappers through ros2 run."""

import os
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from ros2run.api import get_executable_paths


def test_only_cpu_wrappers_are_discoverable():
    executables = get_executable_paths(package_name='hermos_cpu_examples')
    assert {Path(path).name for path in executables} == {
        'image_peer', 'cpp_image_reader', 'cpp_image_peer'}
    for path in executables:
        assert os.access(path, os.X_OK)
        assert 'hermos_runtime.cpu' in Path(path).read_text()


def test_launch_and_parameter_files_are_installed():
    share = Path(get_package_share_directory('hermos_cpu_examples'))
    assert (share / 'launch/cpu_pipeline.launch.py').is_file()
    assert '설치 예제 label with spaces' in (share / 'config/image.yaml').read_text()
