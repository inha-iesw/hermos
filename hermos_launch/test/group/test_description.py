"""Peer descriptions resolve ROS settings without starting a ROS process."""

from pathlib import Path

import pytest
from launch import LaunchContext
from launch.substitutions import LaunchConfiguration
from rclpy.utilities import remove_ros_args

from hermos_launch.description import HermosPeer
from hermos_launch.description import peer as module
from hermos_runtime.environment import Installation


@pytest.fixture
def context(tmp_path, monkeypatch):
    hook = tmp_path / 'libhermos_heaphook.so'
    hook.touch()
    library = tmp_path / 'libhermos.so'
    library.touch()
    installation = Installation(hook, library, Path('/dev/hermos_rclpy'), {}, {})
    monkeypatch.setattr(module, 'resolve_installation', lambda **kwargs: installation)
    monkeypatch.setattr(module, 'check_device', lambda installation: None)
    monkeypatch.setattr(module, 'check_extra_preloads', lambda *args: None)
    monkeypatch.setattr(module, 'check_factory_python_origin', lambda *args: None, raising=False)
    monkeypatch.setattr(module, 'command_argv',
                        lambda command: ['/installed/' + command['argv'][0]], raising=False)
    context = LaunchContext()
    context.launch_configurations.update(camera='camera_node', capacity='4096', ros_namespace='/robot')
    return context


def test_gpu_factory_separates_pool_application_and_ros_arguments(context, tmp_path):
    parameters = tmp_path / '한글 parameters.yaml'
    parameters.write_text('/**:\n  ros__parameters:\n    label: camera\n')
    peer = HermosPeer(peer_id='writer', role='gpu', factory='camera:factory',
        pool=dict(blocks=2, block_bytes=LaunchConfiguration('capacity'), max_reads=3,
                  max_message_bytes=4096), arguments=['공백 argument'],
        name=LaunchConfiguration('camera'), namespace='camera',
        remappings=[('image', 'frames')], parameters=[str(parameters)])
    command = peer.resolve(context, {'PATH': '/usr/bin'})
    argv = command['argv']
    assert command['package'] == 'rclpy_hermos_gpu' and argv[0] == 'hermos_gpu_peer'
    assert '--device' not in argv
    assert argv[argv.index('--block-bytes') + 1] == '4096'
    remainder = argv[argv.index('camera:factory') + 1:]
    assert remove_ros_args(['peer', *remainder])[1:] == ['공백 argument']
    assert '__node:=camera_node' in remainder and '__ns:=/robot/camera' in remainder
    assert 'image:=frames' in remainder and str(parameters) in remainder
    assert command['id'] == 'writer' and command['role'] == 'gpu'
    assert command['env']['LD_PRELOAD'].endswith('/libhermos_heaphook.so')
    assert command['env']['HERMOS_DEVICE_PATH'] == '/dev/hermos_rclpy'


def test_direct_metadata_peer_has_no_preload_and_worker_resolves_package(context):
    peer = HermosPeer(peer_id='observer', role='metadata', package='observer_pkg',
                     executable='registered_observer', registered=True, namespace='/outside')
    command = peer.resolve(context, {'LD_PRELOAD': '/unused/payload-hook.so', 'ROS_DOMAIN_ID': '42'})
    assert command['package'] == 'observer_pkg'
    assert command['argv'] == ['registered_observer', '--ros-args', '-r', '__ns:=/outside']
    assert command['env'].get('LD_PRELOAD', '') == ''
    assert command['env']['ROS_DOMAIN_ID'] == '42'
    assert command['env']['HERMOS_BRIDGE_MODE'] == 'off'


@pytest.mark.parametrize('kwargs', [
    dict(role='cpu', package='pkg', executable='ordinary_node'),
    dict(role='gpu', factory='app:factory'),
    dict(role='metadata', factory='app:factory'),
    dict(role='cpu', factory='app:factory', respawn=True),
    dict(role='cpu', factory='app:factory', package='pkg', executable='peer'),
])
def test_unsupported_peer_contracts_fail_before_launch(kwargs):
    with pytest.raises((ValueError, TypeError)):
        HermosPeer(peer_id='peer', **kwargs)


def test_missing_parameter_file_is_not_silently_ignored(context, tmp_path):
    peer = HermosPeer(peer_id='cpu', role='cpu', factory='app:factory',
                     parameters=[str(tmp_path / 'missing.yaml')])
    with pytest.raises(ValueError, match='parameter file'):
        peer.resolve(context, {})


def test_parameter_path_tuple_is_concatenated_instead_of_becoming_scoped_value(context, tmp_path):
    path = tmp_path / 'params.yaml'
    path.write_text('/**:\n  ros__parameters:\n    label: camera\n')
    context.launch_configurations.update(directory=str(tmp_path), global_params=[('mode', 'camera')])
    peer = HermosPeer(peer_id='cpu', role='cpu', factory='app:factory',
                     parameters=[(LaunchConfiguration('directory'), '/params.yaml')])
    argv = peer.resolve(context, {})['argv']
    assert argv[-4:] == ['-p', 'mode:=camera', '--params-file', str(path)]


def test_peer_reuse_preserves_remappings(context):
    peer = HermosPeer(peer_id='cpu', role='cpu', factory='app:factory',
                     remappings=[('image', 'frames')])
    first = peer.resolve(context, {})['argv']
    second = peer.resolve(context, {})['argv']
    assert first == second and 'image:=frames' in second


@pytest.mark.parametrize('role', ['cpu', 'gpu'])
def test_factory_preflight_checks_resolved_entry_with_final_child_environment(context, monkeypatch, role):
    checked = []
    resolved = []

    def executable(command):
        resolved.append(command)
        return ['/selected/lib/' + command['argv'][0], *command['argv'][1:]]

    def check(installation, executable, environment):
        checked.append((executable, dict(environment)))

    monkeypatch.setattr(module, 'command_argv', executable)
    monkeypatch.setattr(module, 'check_factory_python_origin', check)
    peer = HermosPeer(peer_id='peer', role=role, factory='app:factory',
                     pool=dict(block_bytes=64, max_reads=2, max_message_bytes=64) if role == 'gpu' else None,
                     additional_env=dict(PYTHONPATH='/child/python', PYTHONHOME='/child/home',
                                         PATH='/child/bin', AMENT_PREFIX_PATH='/child/overlay'))
    command = peer.resolve(context, dict(PYTHONPATH='/parent/python', PATH='/parent/bin'))
    assert len(resolved) == len(checked) == 1
    assert resolved[0]['env'] == command['env']
    assert checked[0][0] == '/selected/lib/hermos_' + role + '_peer'
    assert checked[0][1] == command['env']
    assert checked[0][1]['PYTHONPATH'] == '/child/python'
    assert checked[0][1]['PYTHONHOME'] == '/child/home'
    assert checked[0][1]['PATH'] == '/child/bin'


def test_direct_native_peer_does_not_require_python_origin(context, monkeypatch):
    def unexpected(*args):
        raise AssertionError('native executable incorrectly required a Python factory')

    monkeypatch.setattr(module, 'check_factory_python_origin', unexpected)
    peer = HermosPeer(peer_id='native', role='cpu', package='cpp_application',
                     executable='registered_cpp_peer', registered=True)
    assert peer.resolve(context, {})['argv'] == ['registered_cpp_peer', '--ros-args', '-r', '__ns:=/robot']
