"""Connect one ROS launch action to the installed epoch supervisor."""

import json
import os
from pathlib import Path
import signal
import tempfile

from launch import Action
from launch.actions import EmitEvent, ExecuteProcess, RegisterEventHandler
from launch.event_handlers import OnProcessExit, OnProcessStart, OnShutdown
from launch.events.process import SignalProcess
from launch.logging import launch_config

from hermos_runtime.supervision.group import command_argv, validate
from hermos_runtime.supervision.isolated import supervisor_shutdown_timeout
from hermos_runtime.supervision.installation import validate_installation
from ..description import HermosPeer
from ..description.peer import resolve_value


def supervisor_executable(environment):
    """Locate the installed supervisor without loading native or GPU code."""
    return command_argv(dict(package='hermos_runtime', argv=['hermos_supervisor'], env=environment))[0]


class HermosPeerGroup(Action):
    """Launch registered CPU/GPU peers in one fresh supervised private epoch.

    ``peers`` contains :class:`HermosPeer` descriptions, never Node actions.
    The installed supervisor owns all peers and any bounded whole-group restart.
    ``startup_timeout``, ``stop_timeout`` and ``max_restarts`` accept launch
    substitutions. Shutdown escalation includes all supervisor cleanup waits;
    the default 10/5 second settings allow 163 seconds before TERM and another
    five seconds before KILL. Unconfirmed teardown or unexpected supervisor exit
    fails the launch instead of being interpreted as a successful restart.

    ``log_directory`` defaults to the ROS launch log directory. A private fresh
    child contains the resolved config and persistent epoch evidence/logs;
    ``stage`` exposes the supervisor stage after execution. Process-start is
    only supervisor start; its registration-ready output is not workload ready.
    ``additional_env`` is scoped to this group. The supervisor and metadata
    peers never preload the payload allocator. A killed launch parent requests
    supervisor drain through Linux parent-death notification.
    """

    def __init__(self, *, peers, startup_timeout=10, stop_timeout=5, max_restarts=1,
                 log_directory=None, additional_env=None, output='screen', condition=None):
        super().__init__(condition=condition)
        self._peers = tuple(peers)
        if not self._peers or any(not isinstance(peer, HermosPeer) for peer in self._peers):
            raise ValueError('peers must contain HermosPeer descriptions')
        if len({peer.peer_id for peer in self._peers}) != len(self._peers):
            raise ValueError('peer IDs must be unique within a group')
        self._startup_timeout, self._stop_timeout = startup_timeout, stop_timeout
        self._max_restarts, self._log_directory = max_restarts, log_directory
        self._additional_env, self._output = dict(additional_env or {}), output
        self.stage = self.process = self.result = None

    def _signal_supervisor(self):
        return EmitEvent(event=SignalProcess(
            signal_number=signal.SIGINT, process_matcher=lambda process: process is self.process))

    def _started(self, event, context):
        # Register only after transport creation. If shutdown raced startup, send
        # INT now; otherwise interactive launch may assume tty delivered it.
        handlers = [RegisterEventHandler(OnShutdown(on_shutdown=self._shutdown))]
        return [*handlers, self._signal_supervisor()] if context.is_shutdown else handlers

    def _shutdown(self, event, context):
        if event.due_to_sigint and not context.noninteractive:
            return [self._signal_supervisor()]

    def _exited(self, event, context):
        try:
            self.result = json.loads((self.stage / 'cli.json').read_text())
        except (OSError, ValueError) as error:
            raise RuntimeError('HERM0S supervisor left no valid termination evidence: ' +
                               str(self.stage)) from error
        expected_exit = event.returncode == 0 or (
            context.is_shutdown and event.returncode in (130, 143))
        if (not expected_exit or self.result.get('exit_code') != event.returncode or
                not self.result.get('verified_teardown') or self.result.get('state') != 'stopped' or
                self.result.get('peer_failed') is not False):
            raise RuntimeError('HERM0S group failed or teardown is incomplete; inspect ' + str(self.stage))

    def execute(self, context):
        """Resolve configuration, then execute exactly one direct supervisor."""
        if self.stage is not None:
            raise RuntimeError('a HermosPeerGroup action can only execute once')
        startup = float(resolve_value(context, self._startup_timeout))
        stop = float(resolve_value(context, self._stop_timeout))
        restarts = int(resolve_value(context, self._max_restarts))
        if restarts < 0:
            raise ValueError('max_restarts must be nonnegative')
        environment = dict(context.environment)
        environment.update({resolve_value(context, key): resolve_value(context, value)
                            for key, value in self._additional_env.items()})
        validate_installation(environment)
        commands = [peer.resolve(context, environment) for peer in self._peers]
        validate(commands, startup, stop)
        for command in commands:
            command_argv(command)
        environment.pop('LD_PRELOAD', None)
        executable = supervisor_executable(environment)
        directory = Path(launch_config.log_dir if self._log_directory is None else
                         resolve_value(context, self._log_directory)).absolute()
        if directory.resolve().is_relative_to('/dev/shm'):
            raise ValueError('group log_directory must be outside /dev/shm')
        directory.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix='hermos-group-', dir=directory))
        config = root / 'commands.json'
        config.write_text(json.dumps({'version': 1, 'commands': commands}, indent=2) + '\n')
        # The supervisor creates this itself with exist_ok=False, including on restart.
        self.stage = root / 'supervisor'
        self.process = ExecuteProcess(
            cmd=[executable, str(config), '--stage', str(self.stage),
                 '--startup-timeout', str(startup), '--stop-timeout', str(stop),
                 '--max-restarts', str(restarts), '--parent-pid', str(os.getpid())],
            name='hermos_supervisor', env=environment, prefix='', output=self._output,
            sigterm_timeout=str(supervisor_shutdown_timeout(startup, stop)), sigkill_timeout='5')
        return [RegisterEventHandler(OnProcessStart(target_action=self.process, on_start=self._started)),
                RegisterEventHandler(OnProcessExit(target_action=self.process, on_exit=self._exited)),
                self.process]
