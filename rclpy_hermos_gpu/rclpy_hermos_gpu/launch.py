"""Direct registered Python peer entry point with bounded GPU shutdown."""

import argparse
import importlib
import os
import select
import signal
import sys
from threading import Event, Thread
import time
import traceback


class _TerminationDeadline:
    """Bound supported native calls even when the main thread cannot handle TERM.

    CPython writes the signal byte before running its Python handler. The native
    GPU map releases the GIL, so this observer can report failure and exit while
    FD receipt is blocked. This is failure containment, never successful drain.
    """

    def __init__(self):
        self.read_fd, self.write_fd = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self.previous_fd = signal.set_wakeup_fd(self.write_fd)
        self.finished = Event()
        self.thread = Thread(target=self._observe, name='gpu-termination-deadline', daemon=True)
        self.thread.start()

    def _observe(self):
        deadline = None
        while not self.finished.is_set():
            readable, _, _ = select.select([self.read_fd], [], [], 0.02)
            if readable:
                received = os.read(self.read_fd, 4096)
                if deadline is None and any(number in received for number in
                                            (signal.SIGTERM, signal.SIGINT)):
                    deadline = time.monotonic() + 3.0
            if deadline is not None and time.monotonic() >= deadline:
                os.write(2, b'GPU peer TERM deadline expired; teardown incomplete, restart required\n')
                os._exit(70)

    def close(self):
        signal.set_wakeup_fd(self.previous_fd)
        self.finished.set()
        self.thread.join()
        os.close(self.read_fd)
        os.close(self.write_fd)


def _draining_executor(*, context):
    """Create a single-threaded executor with a public-API shutdown progress gate.

    Drain admits existing coroutine continuations and client replies. New entity
    callbacks, including timers, subscriptions, services and Waitables, are blocked.
    A suspended callback depending on one of those new callbacks may time out.
    """
    from rclpy.client import Client
    from rclpy.executors import (ExternalShutdownException, ShutdownException,
                                SingleThreadedExecutor, TimeoutException)

    class DrainingExecutor(SingleThreadedExecutor):
        def __init__(self):
            self._drain_nodes = None
            super().__init__(context=context)

        def can_execute(self, entity):
            return ((self._drain_nodes is None or isinstance(entity, Client)) and
                    super().can_execute(entity))

        def begin_drain(self):
            if self._drain_nodes is None:
                self._drain_nodes = self.get_nodes()

        def progress(self):
            if self._drain_nodes is None:
                raise RuntimeError('executor must begin drain before shutdown progress')
            try:
                # Explicit frozen nodes change the public wait arguments, discarding
                # any readiness generator cached before the admission gate closed.
                task, _, _ = self.wait_for_ready_callbacks(timeout_sec=0, nodes=self._drain_nodes)
            except (TimeoutException, ShutdownException, ExternalShutdownException):
                return
            task()
            if task.done():
                failure = task.exception()
                if failure is not None:
                    raise failure

    return DrainingExecutor()


def main(argv=None):
    """Initialize one GPU epoch, call ``module:factory(runtime, args)``, then spin.

    The factory returns one rclpy Node or a sequence of Nodes. It creates endpoints
    and timers but must not spin or publish before returning. The direct process
    registers its full inventory before dispatch. TERM stops new dispatch while
    existing coroutine continuations and client replies can return GPU owners.
    Futures requiring new timer/service callbacks may reach the three-second drain
    deadline; busy or poisoned drain exits nonzero. Arbitrary native calls
    that block the interpreter remain subject to the supervisor's failure timeout.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blocks', type=int, default=0)
    parser.add_argument('--block-bytes', type=int, required=True)
    parser.add_argument('--max-reads', type=int, required=True)
    parser.add_argument('--max-message-bytes', type=int, required=True)
    parser.add_argument('--device', type=int, default=0)
    parser.add_argument('factory', help='importable module:function returning Node(s)')
    parser.add_argument('application_args', nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    if ':' not in options.factory:
        parser.error('factory must be module:function')

    # This entry point owns signals before importing application/Torch code or
    # creating CUDA resources. rclpy must not tear its context down first.
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        del signum, frame
        # Raising through rclpy's pybind take_message can replace the signal
        # with a conversion exception. Dispatch returns to this flag instead.
        stopping = True

    previous = {number: signal.signal(number, stop)
                for number in (signal.SIGINT, signal.SIGTERM)}
    runtime = executor = None
    deadline = None
    nodes = []
    status = 0
    context = None
    try:
        import rclpy
        from rclpy.context import Context
        from rclpy.node import Node
        from rclpy.signals import SignalHandlerOptions
        from .runtime import GpuRuntime

        context = Context()
        rclpy.init(args=[], context=context, signal_handler_options=SignalHandlerOptions.NO)
        runtime = GpuRuntime(block_count=options.blocks, block_bytes=options.block_bytes,
                             device=options.device, max_reads=options.max_reads,
                             max_message_bytes=options.max_message_bytes)
        runtime.initialise()
        module, name = options.factory.split(':', 1)
        factory = getattr(importlib.import_module(module), name)
        # Factories use the launcher's context explicitly via this attribute;
        # no global/default rclpy context is implicitly created.
        runtime.ros_context = context
        application = factory(runtime, options.application_args)
        nodes = [application] if isinstance(application, Node) else list(application)
        if not nodes or any(not isinstance(node, Node) for node in nodes):
            raise TypeError('GPU peer factory must return rclpy Node(s)')
        # Hermos init installs native handlers during endpoint setup. Keep TERM
        # under launcher control so its workers survive until ordered owner drain.
        for number in previous:
            signal.signal(number, stop)
        executor = _draining_executor(context=context)
        for node in nodes:
            executor.add_node(node)
        # Start after endpoint setup, which may create Hermos's unlink daemon.
        deadline = _TerminationDeadline()
        runtime.register()
        while context.ok() and not stopping:
            runtime.poll()
            executor.spin_once(timeout_sec=0.01)
    except BaseException as error:
        traceback.print_exc()
        traceback.clear_frames(error.__traceback__)
        status = 70
    finally:
        # A repeated signal cannot cut the owner teardown in half. The outer
        # supervisor still bounds the process and treats KILL escalation as failure.
        for number in previous:
            signal.signal(number, signal.SIG_IGN)
        if runtime is not None:
            try:
                progress = None
                if executor is not None:
                    executor.begin_drain()
                    progress = executor.progress
                runtime.shutdown(timeout=3.0, progress=progress)
            except BaseException:
                traceback.print_exc()
                status = 70
        if executor is not None:
            try:
                executor.shutdown(timeout_sec=0)
            except BaseException:
                traceback.print_exc()
                status = 70
        for node in nodes:
            try:
                node.destroy_node()
            except BaseException:
                traceback.print_exc()
                status = 70
        if context is not None and context.ok():
            context.shutdown()
        if deadline is not None:
            deadline.close()
        for number, handler in previous.items():
            signal.signal(number, handler)
    return status


if __name__ == '__main__':
    sys.exit(main())
