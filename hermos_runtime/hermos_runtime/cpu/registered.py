"""Run one direct CPU peer only after its supervisor acknowledges registration."""

import argparse
import importlib
import signal
import sys
import traceback


def main(argv=None):
    """Call ``module:factory(context, args)``, register its endpoints, then spin.

    The factory returns one rclpy Node or a sequence of Nodes using the supplied
    context. It creates endpoints but must not publish, spin or start background
    dispatch before returning. Application arguments exclude ROS arguments;
    remaps and parameters are passed to the owned context. The supervisor or CPU
    bootstrap must prepare the process environment before invoking this entry.

    This entry requires supervisor registration and owns executor/node/context
    cleanup. Factories must release partial resources if construction raises.
    Callbacks and retained aliases must finish before returning from shutdown;
    incomplete executor shutdown or any cleanup error returns a nonzero status.
    Native calls that block Python remain bounded by the owning supervisor.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('factory', help='module:function returning Node(s)')
    parser.add_argument('application_args', nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    module, separator, function = options.factory.partition(':')
    if not separator or not module or not function.isidentifier():
        parser.error('factory must be module:function')

    stopping = False

    def stop(number, frame):
        nonlocal stopping
        stopping = True

    previous = {number: signal.signal(number, stop)
                for number in (signal.SIGINT, signal.SIGTERM)}
    context = executor = None
    nodes = []
    status = 0
    try:
        from ..supervision.registration import Registration

        registration = Registration.from_environment(required=True)
        import rclpy
        from rclpy.context import Context
        from rclpy.executors import ExternalShutdownException, SingleThreadedExecutor
        from rclpy.node import Node
        from rclpy.signals import SignalHandlerOptions
        from rclpy.utilities import remove_ros_args

        application_args = remove_ros_args([options.factory, *options.application_args])[1:]
        context = Context()
        rclpy.init(args=options.application_args, context=context,
                   signal_handler_options=SignalHandlerOptions.NO)
        factory = getattr(importlib.import_module(module), function)
        application = factory(context, application_args)
        returned = [application] if isinstance(application, Node) else list(application)
        nodes = [node for node in returned if isinstance(node, Node)]
        if not nodes or len(nodes) != len(returned):
            raise TypeError('CPU peer factory must return rclpy Node(s)')
        if any(node.context is not context for node in nodes):
            raise ValueError('CPU peer nodes must use the supplied context')
        # Endpoint initialization may replace native handlers; reclaim signal
        # ownership before ACK so teardown precedes context invalidation.
        for number in previous:
            signal.signal(number, stop)
        executor = SingleThreadedExecutor(context=context)
        for node in nodes:
            executor.add_node(node)
        registration.wait([])
        while context.ok() and not stopping:
            try:
                executor.spin_once(timeout_sec=0.01)
            except ExternalShutdownException:
                if context.ok():
                    raise
                break
    except BaseException as error:
        traceback.print_exc()
        traceback.clear_frames(error.__traceback__)
        status = 70
    finally:
        # Repeated signals cannot cut cleanup in half. The supervisor retains
        # its timeout and treats forced termination as incomplete teardown.
        for number in previous:
            signal.signal(number, signal.SIG_IGN)
        if executor is not None:
            try:
                if not executor.shutdown(timeout_sec=0):
                    raise RuntimeError('CPU executor shutdown is incomplete')
            except BaseException:
                traceback.print_exc()
                status = 70
        for node in nodes:
            try:
                node.destroy_node()
            except BaseException:
                traceback.print_exc()
                status = 70
        if context is not None:
            try:
                if context.ok():
                    context.shutdown()
            except BaseException:
                traceback.print_exc()
                status = 70
        for number, handler in previous.items():
            signal.signal(number, handler)
    return status


if __name__ == '__main__':
    sys.exit(main())
