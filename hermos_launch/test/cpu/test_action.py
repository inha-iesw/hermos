"""Exercise real LaunchService/ROS argument parsing without a shared-heap device."""

import json
import os
from pathlib import Path
import subprocess
import sys

from launch import LaunchDescription, LaunchService
from launch.actions import GroupAction, SetEnvironmentVariable
from launch.substitutions import EnvironmentVariable
from launch_ros.actions import Node

from hermos_launch.actions import HermosNode
from hermos_launch.actions import cpu
from hermos_runtime.environment import Installation


def test_ros_arguments_and_scoped_environment_reach_only_selected_peer(tmp_path, monkeypatch):
    # This library has no allocator interposition: the test covers launch argv/env,
    # while installed CPU wrapper and real allocator checks belong to device tests.
    source = tmp_path / 'noop.c'
    source.write_text('int example_noop(void) { return 0; }\n')
    hook = tmp_path / 'libhermos_heaphook.so'
    subprocess.run(['cc', '-shared', '-fPIC', str(source), '-o', str(hook)], check=True)
    library = tmp_path / 'libhermos.so'
    library.write_bytes(hook.read_bytes())
    selected = []

    def resolve(*, providers, environment):
        selected.append((tuple(providers), environment['PROBE_SCOPED']))
        return Installation(hook, library, Path('/dev/hermos_rclpy'), {}, {})

    monkeypatch.setattr(cpu, 'resolve_installation', resolve)
    monkeypatch.setattr(cpu, 'check_device', lambda installation: None, raising=False)
    monkeypatch.delenv('LD_PRELOAD', raising=False)
    monkeypatch.setenv('ROS_LOG_DIR', str(tmp_path / 'ros-log'))
    config = tmp_path / 'parameters with spaces.yaml'
    config.write_text('/**:\n  ros__parameters:\n    label: "한글 parameter with spaces"\n')
    probe = str(Path(__file__).with_name('probe.py'))
    output, ordinary = tmp_path / 'peer.json', tmp_path / 'dds.json'
    arguments = ['--output', str(output), '--tag', '공백 argument']
    peer = HermosNode(executable=sys.executable, name='camera_writer', namespace='camera',
        arguments=[probe, *arguments], parameters=[str(config)], remappings=[('image', 'frames')],
        providers=['rclpy_hermos_adapters'], output='screen',
        additional_env={'PROBE_SCOPED': EnvironmentVariable('PROBE_SCOPED')})
    dds = Node(executable=sys.executable, name='dds_control',
        arguments=[probe, '--output', str(ordinary), '--tag', 'DDS'], output='screen')
    service = LaunchService()
    service.include_launch_description(LaunchDescription([
        GroupAction([SetEnvironmentVariable('PROBE_SCOPED', 'scope value'), peer]), dds]))
    assert service.run() == 0
    result = json.loads(output.read_text())
    assert result['name'] == 'camera_writer'
    assert result['namespace'] == '/camera'
    assert result['topic'] == '/camera/frames'
    assert result['label'] == '한글 parameter with spaces'
    assert result['tag'] == '공백 argument'
    assert result['scoped'] == 'scope value'
    assert result['device'] == '/dev/hermos_rclpy'
    # The wrapper, not launch or an intermediate interpreter, preloads the final app.
    assert result['preload'] == ''
    assert selected == [(('rclpy_hermos_adapters',), 'scope value')]
    control = json.loads(ordinary.read_text())
    assert control['preload'] is None and control['device'] is None
    assert control['scoped'] is None
    assert 'LD_PRELOAD' not in os.environ
