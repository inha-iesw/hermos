"""Run three registered CPU/GPU peers alongside an ordinary DDS Node."""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from hermos_launch.actions import HermosPeerGroup
from hermos_launch.description import HermosPeer


def pipeline(context):
    output = Path(LaunchConfiguration('output_directory').perform(context)).absolute()
    output.mkdir(parents=True, exist_ok=False)
    configuration = str(Path(get_package_share_directory('hermos_gpu_examples')) /
                        'config/pipeline.yaml')
    profile = str(Path(get_package_share_directory('rclpy_hermos_gpu')) /
                  'config/dds/fastdds_udp.xml')
    environment = dict(RMW_IMPLEMENTATION='rmw_fastrtps_cpp',
                       FASTRTPS_DEFAULT_PROFILES_FILE=profile)
    common = dict(namespace='camera', parameters=[configuration])
    peers = [HermosPeer(
        peer_id=role, role='gpu', factory='hermos_gpu_examples.pipeline.peer:tensor_factory',
        arguments=[role, str(output)], name='gpu_' + role, remappings=[('tensor', 'frames')],
        pool=dict(blocks=3 if role == 'writer' else 0, block_bytes=64,
                  max_reads=4, max_message_bytes=64), **common) for role in ('writer', 'reader')]
    peers.append(HermosPeer(
        peer_id='cpu', role='cpu', factory='hermos_gpu_examples.pipeline.peer:cpu_factory',
        arguments=[str(output)], name='cpu_peer', remappings=[('image', 'cpu_frames')],
        providers=['rclpy_hermos_adapters'], **common))
    return [
        HermosPeerGroup(peers=peers, log_directory=str(output), startup_timeout=30,
                        stop_timeout=5, max_restarts=0, additional_env=environment),
        Node(package='hermos_gpu_examples', executable='dds_control',
             arguments=[str(output)], name='dds_control', additional_env=environment,
             output='screen', **common),
    ]


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('output_directory', description='A new directory for pipeline evidence'),
        OpaqueFunction(function=pipeline),
    ])
