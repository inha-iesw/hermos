"""Resolve a registered peer's arguments and process-local environment."""

from pathlib import Path
import re

from launch.utilities import normalize_to_list_of_substitutions, perform_substitutions
from launch_ros.utilities import make_namespace_absolute, normalize_remap_rules, prefix_namespace
from rclpy.validate_namespace import validate_namespace
from rclpy.validate_node_name import validate_node_name

from hermos_runtime.environment import peer_environment, resolve_installation
from hermos_runtime.environment.peer import check_extra_preloads
from hermos_runtime.environment.process import check_device, check_factory_python_origin
from hermos_runtime.supervision.factory import parse_factory
from hermos_runtime.supervision.group import command_argv


def resolve_value(context, value):
    """Resolve one argument, preserving spaces and accepting numeric settings."""
    if isinstance(value, (int, float, Path)):
        value = str(value)
    return perform_substitutions(context, normalize_to_list_of_substitutions(value))


class HermosPeer:
    """Describe a direct peer owned by a :class:`HermosPeerGroup` supervisor.

    Supply either ``factory='module:function'`` or ``package``/``executable``.
    Factories use the registered CPU or GPU launcher. An executable requires
    ``registered=True``: it must implement the registration/ACK and shutdown
    contract itself, without a shell, ``ros2 run``, CPU bootstrap wrapper, or
    background dispatch before ACK. The worker resolves it in the package's
    installed ``lib/<package>`` directory and directly owns the resulting PID.

    ``role`` is ``cpu``, ``gpu`` or ``metadata``; only payload roles preload the
    heap hook. Metadata executables still register an empty inventory. GPU
    factories require a ``pool`` mapping with ``block_bytes``, ``max_reads`` and
    ``max_message_bytes``; ``blocks`` and ``device`` default to zero. Capacity
    is fixed before initialization, never resized through ROS parameters.

    Arguments, ROS name/namespace/remappings, parameter file paths and pool
    values accept launch substitutions. ``parameters`` accepts existing YAML
    files, not inline dictionaries. ROS settings also honor scoped namespace,
    remaps and parameters. Only the supervisor may restart the whole group.
    """

    def __init__(self, *, peer_id, role, package=None, executable=None, factory=None,
                 registered=False, arguments=(), name=None, namespace=None,
                 remappings=(), parameters=(), pool=None, providers=(), additional_env=None):
        if not isinstance(peer_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', peer_id):
            raise ValueError('peer_id must be a nonempty name using letters, digits, _ or -')
        if role not in ('cpu', 'gpu', 'metadata'):
            raise ValueError('role must be cpu, gpu or metadata')
        if factory is not None:
            if package is not None or executable is not None or role == 'metadata':
                raise ValueError('factory requires a CPU/GPU role and no package/executable')
        elif package is None or executable is None or registered is not True:
            raise ValueError('package/executable requires the direct registered=True contract')
        if role == 'gpu' and factory is not None:
            required = {'block_bytes', 'max_reads', 'max_message_bytes'}
            if (not isinstance(pool, dict) or not required <= pool.keys() or
                    pool.keys() - required - {'blocks', 'device'}):
                raise ValueError('GPU factory requires explicit pool capacity and message limits')
        elif pool is not None:
            raise ValueError('pool settings belong to the supported GPU factory launcher')
        self.peer_id, self.role = peer_id, role
        self.package, self.executable, self.factory = package, executable, factory
        self.arguments, self.name, self.namespace = tuple(arguments), name, namespace
        self.remappings = list(normalize_remap_rules(remappings))
        self.parameters, self.pool = tuple(parameters), dict(pool or {})
        self.providers, self.additional_env = tuple(providers), dict(additional_env or {})

    def _ros_arguments(self, context):
        result = []
        if self.name is not None:
            name = resolve_value(context, self.name)
            validate_node_name(name)
            result.extend(['-r', '__node:=' + name])
        namespace = None if self.namespace is None else resolve_value(context, self.namespace)
        namespace = make_namespace_absolute(prefix_namespace(
            context.launch_configurations.get('ros_namespace'), namespace))
        if namespace is not None:
            validate_namespace(namespace)
            result.extend(['-r', '__ns:=' + namespace])
        parameters = [(True, value) for value in context.launch_configurations.get('global_params', [])]
        parameters.extend((False, value) for value in self.parameters)
        for scoped, parameter in parameters:
            if scoped and isinstance(parameter, tuple):
                # Scoped SetParameter stores an already evaluated name/value pair.
                key, value = parameter
                result.extend(['-p', f'{key}:={value}'])
                continue
            if isinstance(parameter, dict):
                raise ValueError('parameters accepts YAML parameter files, not inline dictionaries')
            path = Path(resolve_value(context, parameter)).absolute()
            if not path.is_file():
                raise ValueError('parameter file does not exist: ' + str(path))
            result.extend(['--params-file', str(path)])
        remappings = [*context.launch_configurations.get('ros_remaps', []), *self.remappings]
        for source, target in remappings:
            result.extend(['-r', resolve_value(context, source) + ':=' + resolve_value(context, target)])
        return ['--ros-args', *result] if result else []

    def resolve(self, context, environment):
        """Return the worker command after validation; do not import the application."""
        command = {'id': self.peer_id, 'role': self.role}
        if self.factory is None:
            command['package'] = resolve_value(context, self.package)
            argv = [resolve_value(context, self.executable)]
        else:
            factory = resolve_value(context, self.factory)
            parse_factory(factory)
            if self.role == 'cpu':
                command['package'] = 'hermos_runtime'
                argv = ['hermos_cpu_peer']
            else:
                command['package'] = 'rclpy_hermos_gpu'
                argv = ['hermos_gpu_peer']
                for key, value in self.pool.items():
                    value = int(resolve_value(context, value))
                    if value < (0 if key in ('blocks', 'device') else 1):
                        raise ValueError('invalid GPU pool setting: ' + key)
                    argv.extend(['--' + key.replace('_', '-'), str(value)])
            argv.append(factory)
        command['argv'] = [*argv, *(resolve_value(context, arg) for arg in self.arguments),
                           *self._ros_arguments(context)]
        prepared = dict(environment)
        prepared.update({resolve_value(context, key): resolve_value(context, value)
                         for key, value in self.additional_env.items()})
        if self.role == 'metadata':
            prepared.pop('LD_PRELOAD', None)
            prepared.update(HERMOS_BRIDGE_MODE='off', HERMOS_NO_DISCOVERY_AGENT='1')
        else:
            installation = resolve_installation(
                providers=[resolve_value(context, item) for item in self.providers], environment=prepared)
            check_device(installation)
            prepared = peer_environment(installation, prepared)
            check_extra_preloads(installation, prepared)
        command['env'] = prepared
        if self.factory is not None:
            check_factory_python_origin(installation, command_argv(command)[0], prepared)
        return command
