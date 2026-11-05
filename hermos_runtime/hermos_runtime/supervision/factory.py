"""Shared factory/context contracts, importable before loading ROS or native code."""


def parse_factory(spec):
    """Return a dotted module and function identifier, or raise ``ValueError``."""
    if not isinstance(spec, str):
        raise ValueError('factory must be module:function')
    module, separator, function = spec.partition(':')
    if (not separator or not all(part.isidentifier() for part in module.split('.')) or
            not function.isidentifier()):
        raise ValueError('factory must be module:function')
    return module, function


def initialise_context(context, arguments):
    """Initialize a caller-owned ROS context and return only application arguments.

    The caller retains ``context`` before invoking this function so partially
    initialized resources remain available to its cleanup path if init raises.
    Signals belong to the peer launcher throughout setup and shutdown.
    """
    import rclpy
    from rclpy.signals import SignalHandlerOptions
    from rclpy.utilities import remove_ros_args

    application_args = remove_ros_args(['hermos_peer', *arguments])[1:]
    rclpy.init(args=arguments, context=context, signal_handler_options=SignalHandlerOptions.NO)
    return application_args


def require_nodes(application, context, *, owned_nodes):
    """Validate factory output while retaining every yielded Node for cleanup.

    ``owned_nodes`` is the caller's initially empty cleanup list. Valid Node
    objects are appended before context checks, including nodes yielded before
    an iterator raises or alongside invalid values. The caller destroys these
    nodes on failure but only shuts down its own context.
    """
    from rclpy.node import Node

    returned = [application] if isinstance(application, Node) else application
    invalid = False
    for node in returned:
        if isinstance(node, Node):
            owned_nodes.append(node)
        else:
            invalid = True
    if invalid or not owned_nodes:
        raise TypeError('peer factory must return rclpy Node(s)')
    if any(node.context is not context for node in owned_nodes):
        raise ValueError('peer nodes must use the supplied context')
