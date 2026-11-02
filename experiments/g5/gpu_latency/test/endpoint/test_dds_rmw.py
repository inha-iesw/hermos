"""Load each installed RMW typesupport by constructing real DDS endpoints, without CUDA."""

import os
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize('rmw', ['rmw_cyclonedds_cpp', 'rmw_fastrtps_cpp'])
def test_real_node_constructs_gpu_descriptor_endpoints_for_each_rmw(rmw):
    program = textwrap.dedent('''\
        import rclpy
        from rclpy.context import Context
        from rclpy.node import Node
        from rclpy_hermos_gpu import GpuRuntime
        from experiments.g5.gpu_latency.dds_endpoint import DDSPublisher, DDSSubscription

        context = Context()
        rclpy.init(context=context)
        node = Node('g5_dds_typesupport_check', context=context)
        runtime = GpuRuntime(block_count=0, block_bytes=4096, device=0,
                             max_reads=4, max_message_bytes=4096)
        try:
            publisher = DDSPublisher(node, '/g5/descriptor', 4, runtime)
            subscription = DDSSubscription(node, '/g5/descriptor', lambda _: None, 4, runtime)
            assert runtime._endpoints == {publisher, subscription}
            assert runtime._native is None
            print('DDS endpoints created without CUDA', flush=True)
        finally:
            node.destroy_node()
            context.shutdown()
    ''')
    environment = dict(os.environ, RMW_IMPLEMENTATION=rmw, ROS_LOCALHOST_ONLY='1',
                       ROS_DOMAIN_ID=str(170 + os.getpid() % 30))
    result = subprocess.run([sys.executable, '-c', program], env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'DDS endpoints created without CUDA' in result.stdout
