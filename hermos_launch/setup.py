from setuptools import find_packages, setup

setup(
    name='hermos_launch', version='0.1.0', packages=find_packages(),
    data_files=[('share/ament_index/resource_index/packages', ['resource/hermos_launch']),
                ('share/hermos_launch', ['package.xml', 'README.md'])],
    install_requires=['setuptools'], zip_safe=True,
    maintainer='rclpy zerocopy maintainers', maintainer_email='maintainers@example.com',
    description='ROS launch actions for HERM0S CPU applications and registered peer groups.',
    license='Apache-2.0',
)
