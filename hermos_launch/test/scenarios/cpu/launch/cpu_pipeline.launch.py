"""A bounded two-process Image pipeline using installed CPU wrappers."""

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration

from hermos_launch.actions import HermosNode


def generate_launch_description():
    config = get_package_share_directory('hermos_cpu_examples') + '/config/image.yaml'
    common = dict(package='hermos_cpu_examples', namespace=LaunchConfiguration('namespace'),
                  parameters=[config], remappings=[('image', 'frames')],
                  providers=['rclpy_hermos_adapters'], output='screen')
    return LaunchDescription([
        DeclareLaunchArgument('namespace', default_value='camera'),
        HermosNode(executable='image_peer', name='reader',
                   arguments=['--role', 'subscriber', '--tag', 'reader 공백 argument'], **common),
        HermosNode(executable='image_peer', name='writer',
                   arguments=['--role', 'publisher', '--tag', 'writer 공백 argument'], **common),
    ])
