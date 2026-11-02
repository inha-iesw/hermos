"""Actual CUDA terminal-error and FD-stall probes for the direct launcher."""

import os
from pathlib import Path
import socket
from threading import Thread

import torch
from rclpy.node import Node


def factory(runtime, arguments):
    scenario, marker_name = arguments
    marker = Path(marker_name)
    node = Node('g4_control', context=runtime.ros_context)
    stream = torch.cuda.Stream()
    node.gpu_stream = stream
    listener = None
    if scenario == 'stall':
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = Path(os.environ['G4_STALL_SOCKET'])
        listener.bind(str(path))
        listener.listen(1)
        node.gpu_test_listener = listener

        def accept_without_reply():
            connection, _ = listener.accept()
            node.gpu_test_connection = connection
            marker.write_text('FD request stalled\n')
        # No Hermos endpoints are created in this failure probe.
        Thread(target=accept_without_reply, daemon=True).start()

    def trigger():
        timer.cancel()
        native = runtime._native
        loan = native.borrow([64], 1, 8, stream.cuda_stream, 1)
        if scenario == 'poison':
            loan.cancel()
            marker.write_text('query failure armed\n')
            # No more callbacks. The main launcher poll must report the fault.
            return
        descriptor = loan.prepare()
        descriptor['core']['vmm_socket_path'] = str(path)
        native.map(descriptor, stream.cuda_stream, 2)
        raise AssertionError('stalled FD handoff unexpectedly returned')

    timer = node.create_timer(0.02, trigger)
    return node
