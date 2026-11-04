"""Standard ROS Node argument handling with a process-local CPU environment."""

from launch_ros.actions import Node

from hermos_runtime.environment import peer_environment, resolve_installation
from hermos_runtime.environment.peer import check_extra_preloads
from hermos_runtime.environment.process import check_device


class HermosNode(Node):
    """Launch an installed CPU wrapper with normal ROS Node options.

    ``package``, ``executable``, name, namespace, remappings, parameters and
    arguments have the standard ``launch_ros.actions.Node`` meaning. Optional
    ``providers`` names adapter packages to check before starting the process.
    Environment substitutions are resolved in the launch context; only this
    process's environment is modified. The wrapper inserts the heap hook only
    when it execs the final application, avoiding duplicate PID registration.
    It checks the actual allocator before importing Python application code.
    No GPU epoch is created.
    """

    def __init__(self, *, providers=(), **kwargs):
        self._hermos_providers = tuple(providers)
        super().__init__(**kwargs)

    def prepare(self, context):
        """Validate the resolved installation and prepare this peer's environment."""
        super().prepare(context)
        environment = self.process_description.final_env
        installation = resolve_installation(
            providers=self._hermos_providers, environment=environment)
        prepared = peer_environment(installation, environment)
        # The heap hook registers a PID before main. A native wrapper must run
        # without it, then exec the final executable with the hook exactly once.
        check_device(installation)
        check_extra_preloads(installation, prepared)
        prepared['LD_PRELOAD'] = ':'.join(
            item for item in prepared['LD_PRELOAD'].split(':')
            if item != str(installation.heaphook))
        environment.update(prepared)
