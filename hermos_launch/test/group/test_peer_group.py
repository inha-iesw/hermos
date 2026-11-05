"""Real launch lifecycle with an owned host group in place of privileged isolation."""

import json
from pathlib import Path
import signal
import sys
import time

import pytest
from launch import LaunchContext, LaunchDescription, LaunchService
from launch.actions import EmitEvent, GroupAction, OpaqueFunction, SetEnvironmentVariable, TimerAction
from launch.events import Shutdown
from launch.substitutions import EnvironmentVariable
from launch_ros.actions import Node, PushRosNamespace

from hermos_launch.actions import HermosPeerGroup
from hermos_launch.actions import group as module
from hermos_launch.description import HermosPeer


@pytest.fixture
def host_supervisor(monkeypatch, tmp_path):
    monkeypatch.setenv('ROS_LOG_DIR', str(tmp_path / 'ros-log'))
    monkeypatch.delenv('LD_PRELOAD', raising=False)
    script = Path(__file__).with_name('host_supervisor.py')
    monkeypatch.setattr(module, 'supervisor_executable', lambda environment: str(script))


def test_launch_passes_ros_settings_and_only_supervisor_owns_registered_peers(
        host_supervisor, tmp_path):
    config = tmp_path / 'parameters with spaces.yaml'
    config.write_text('/**:\n  ros__parameters:\n    label: "한글 parameter"\n')
    probe = Path(__file__).with_name('registered_probe.py')
    output, ordinary = tmp_path / 'peer.json', tmp_path / 'dds.json'
    # A package lookup is performed inside the host worker, never through ros2 run.
    prefix = tmp_path / 'prefix'
    marker = prefix / 'share/ament_index/resource_index/packages/host_peers'
    marker.parent.mkdir(parents=True)
    marker.touch()
    executable = prefix / 'lib/host_peers/probe'
    executable.parent.mkdir(parents=True)
    executable.symlink_to(probe)
    peer = HermosPeer(peer_id='observer', role='metadata', registered=True,
        package='host_peers', executable='probe', arguments=[str(output), '공백 argument'],
        name='camera', namespace='sensor', parameters=[str(config)], remappings=[('image', 'frames')],
        additional_env={'PROBE_SCOPED': EnvironmentVariable('PROBE_SCOPED')})
    group = HermosPeerGroup(peers=[peer], log_directory=str(tmp_path),
                           startup_timeout='3', stop_timeout='0.2', max_restarts=0,
                           additional_env={'AMENT_PREFIX_PATH': [str(prefix), ':',
                               EnvironmentVariable('AMENT_PREFIX_PATH')]})
    dds_probe = Path(__file__).parents[1] / 'cpu/probe.py'
    dds = Node(executable=sys.executable, arguments=[str(dds_probe), '--output', str(ordinary),
                                                    '--tag', 'dds'], output='screen')
    service = LaunchService()
    service.include_launch_description(LaunchDescription([
        GroupAction([SetEnvironmentVariable('PROBE_SCOPED', 'scope value'),
                     PushRosNamespace('robot'), group]), dds]))
    assert service.run() == 0
    result = json.loads(output.read_text())
    assert result['name'] == 'camera' and result['namespace'] == '/robot/sensor'
    assert result['topic'] == '/robot/sensor/frames' and result['label'] == '한글 parameter'
    assert result['tag'] == '공백 argument' and result['scoped'] == 'scope value'
    assert result['registered'] and result['preload'] is None
    control = json.loads(ordinary.read_text())
    assert control['scoped'] is None and control['device'] is None and control['preload'] is None
    summary = json.loads((group.stage / 'cli.json').read_text())
    assert summary['verified_teardown'] and summary['state'] == 'stopped'
    assert (group.stage / 'epoch-0/group/epoch-0/peer-0.stdout').is_file()


@pytest.mark.parametrize('mode', ['valid', 'failure', 'incomplete', 'state', 'code', 'signal', 'missing', 'peer'])
def test_each_termination_contract_is_checked_independently(host_supervisor, tmp_path, mode):
    peer = HermosPeer(peer_id='observer', role='metadata', package='hermos_runtime',
                     executable='hermos_cpu_peer', registered=True)
    group = HermosPeerGroup(peers=[peer], log_directory=str(tmp_path), max_restarts=0,
                           additional_env={'HOST_SUPERVISOR_MODE': mode})
    service = LaunchService()
    service.include_launch_description(LaunchDescription([group]))
    assert (service.run() == 0) == (mode == 'valid')
    assert group.stage is not None


def test_bad_second_executable_fails_before_creating_supervisor_or_stage(tmp_path):
    peers = [HermosPeer(peer_id='valid', role='metadata', package='hermos_runtime',
                       executable='hermos_cpu_peer', registered=True),
             HermosPeer(peer_id='missing', role='metadata', package='hermos_runtime',
                       executable='not_installed', registered=True)]
    group = HermosPeerGroup(peers=peers, log_directory=str(tmp_path))
    with pytest.raises(ValueError, match='not_installed'):
        group.execute(LaunchContext())
    assert group.process is None and group.stage is None


def test_duplicate_peer_ids_and_nested_respawn_are_rejected():
    peer = HermosPeer(peer_id='cpu', role='cpu', factory='app:factory')
    with pytest.raises(ValueError, match='unique'):
        HermosPeerGroup(peers=[peer, peer])
    with pytest.raises(TypeError):
        HermosPeerGroup(peers=[peer], respawn=True)


def test_group_rejects_runtime_overlay_skew_before_creating_stage(tmp_path):
    prefix = tmp_path / 'overlay'
    marker = prefix / 'share/ament_index/resource_index/packages/hermos_runtime'
    marker.parent.mkdir(parents=True)
    marker.touch()
    executable = prefix / 'lib/hermos_runtime/hermos_supervisor'
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o755)
    peer = HermosPeer(peer_id='metadata', role='metadata', package='peer_pkg',
                     executable='peer', registered=True)
    group = HermosPeerGroup(peers=[peer], log_directory=str(tmp_path),
                           additional_env={'AMENT_PREFIX_PATH': str(prefix)})
    context = LaunchContext()
    with pytest.raises(RuntimeError, match='installation|manifest'):
        group.execute(context)
    assert group.process is None and group.stage is None


def test_interactive_sigint_shutdown_drains_without_waiting_for_term(host_supervisor, tmp_path):
    prefix = tmp_path / 'prefix'
    marker = prefix / 'share/ament_index/resource_index/packages/host_peers'
    marker.parent.mkdir(parents=True)
    marker.touch()
    executable = prefix / 'lib/host_peers/probe'
    executable.parent.mkdir(parents=True)
    executable.symlink_to(Path(__file__).with_name('registered_probe.py'))
    output = tmp_path / 'peer.json'
    peer = HermosPeer(peer_id='observer', role='metadata', package='host_peers', executable='probe',
                     registered=True, arguments=[str(output), 'signal test', '0.1'])
    group = HermosPeerGroup(peers=[peer], log_directory=str(tmp_path), startup_timeout=3,
                           stop_timeout=1, max_restarts=0, additional_env={
                               'AMENT_PREFIX_PATH': [str(prefix), ':', EnvironmentVariable('AMENT_PREFIX_PATH')]})
    requested = []

    def shutdown_when_registered(context):
        if output.is_file():
            requested.append(time.monotonic())
            # Interactive launch assumes tty SIGINT also reached its children.
            # Here only the launch event exists, as with kill -INT <launch-pid>.
            return [EmitEvent(event=Shutdown(due_to_sigint=True))]
        return [TimerAction(period=0.05, actions=[OpaqueFunction(function=shutdown_when_registered)])]

    service = LaunchService(noninteractive=False)
    service.include_launch_description(LaunchDescription([
        group, OpaqueFunction(function=shutdown_when_registered)]))
    assert service.run() == 0
    assert requested and time.monotonic() - requested[0] < 3
    assert output.with_suffix('.drained').is_file()
    assert group.result['exit_code'] == 128 + signal.SIGINT
    assert group.result['verified_teardown'] and not group.result['peer_failed']
