"""Supervise one complete group of directly owned GPU peer processes."""

import ctypes
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import time
import uuid


class RecoveryError(RuntimeError):
    """Recovery stopped because ownership or complete cleanup is unproven."""


def _die_with_parent(parent_pid):
    # Group is single-threaded. Set this before exec and close the fork/prctl
    # race. Forked unlink daemons do not inherit PDEATHSIG and exit naturally.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), signal.SIGKILL)


def validate(commands, startup_timeout, stop_timeout):
    """Validate a command group before creating a stage or launching anything."""
    if (not isinstance(commands, list) or not commands or
            any(not isinstance(item, dict) or not isinstance(item.get('argv'), list) or
                not item['argv'] or any(not isinstance(arg, str) for arg in item['argv']) or
                not item['argv'][0] or not isinstance(item.get('env', {}), dict) or
                any(not isinstance(k, str) or not isinstance(v, str)
                    for k, v in item.get('env', {}).items()) for item in commands)):
        raise ValueError('commands must contain nonempty argv lists and string environments')
    if any(not math.isfinite(value) or value <= 0 for value in (startup_timeout, stop_timeout)):
        raise ValueError('timeouts must be positive and finite')
    for command in commands:
        if command.keys() - {'argv', 'env', 'package', 'id', 'role'}:
            raise ValueError('unknown command fields')
        if 'id' in command and (not isinstance(command['id'], str) or
                               not re.fullmatch(r'[A-Za-z0-9_-]+', command['id'])):
            raise ValueError('peer id must use letters, digits, _ or -')
        if 'role' in command and command['role'] not in ('cpu', 'gpu', 'metadata'):
            raise ValueError('peer role must be cpu, gpu or metadata')
        if 'package' in command and (
                not isinstance(command['package'], str) or
                not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', command['package']) or
                command['argv'][0] in ('.', '..') or '/' in command['argv'][0]):
            raise ValueError('package commands require a package name and an executable basename')
    if len({command.get('id', str(index)) for index, command in enumerate(commands)}) != len(commands):
        raise ValueError('peer IDs must be unique within a group')
    metadata_domains = set()
    for command in commands:
        if command.get('role') != 'metadata':
            continue
        raw_domain = command.get('env', {}).get('ROS_DOMAIN_ID', os.environ.get('ROS_DOMAIN_ID')) or '0'
        # RCL treats leading zeros as octal; the native runtime parses decimal.
        if not re.fullmatch(r'0|[1-9][0-9]*', raw_domain) or int(raw_domain) > 0xffffffff:
            raise ValueError('metadata ROS_DOMAIN_ID must be canonical decimal in the uint32 range')
        domain = int(raw_domain)
        if domain in metadata_domains:
            raise ValueError(f'only one metadata peer per ROS domain is allowed in a group: {domain}')
        metadata_domains.add(domain)


def validate_config(config, startup_timeout, stop_timeout):
    """Accept legacy unversioned CLI configs as v1; reject unknown schemas."""
    if (not isinstance(config, dict) or config.keys() - {'version', 'commands'} or
            type(config.get('version', 1)) is not int or config.get('version', 1) != 1):
        raise ValueError('expected a version 1 commands configuration')
    validate(config.get('commands'), startup_timeout, stop_timeout)


def command_argv(command):
    """Resolve an installed direct executable in the unprivileged worker.

    Raw argv commands retain their existing meaning. Package commands never
    invoke ros2 run or a shell: the registered PID must be the direct child.
    """
    argv = list(command['argv'])
    if 'package' in command:
        package = command['package']
        environment = {**os.environ, **command.get('env', {})}
        # The isolated -E worker has no ament Python dependencies. Search the
        # resource index using this peer's overlay without mutating os.environ.
        prefix = next((Path(path) for path in environment.get('AMENT_PREFIX_PATH', '').split(os.pathsep)
                       if path and (Path(path) / 'share/ament_index/resource_index/packages' /
                                    package).is_file()), None)
        if prefix is None:
            raise ValueError('installed peer package not found: ' + package)
        executable = prefix / 'lib' / package / argv[0]
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError('installed peer executable is missing or not executable: ' + str(executable))
        argv[0] = str(executable)
    return argv


def _resource(path, pid):
    if not isinstance(path, str):
        raise RecoveryError('resource path must be a string')
    metadata = re.fullmatch(r'/dev/shm/rclpy_zc_gpu_v1_' + str(pid) + r'_\d+', path)
    socket = re.fullmatch(
        r'/tmp/cuda_memory_pool_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.sock', path)
    if not metadata and not socket:
        raise RecoveryError('resource is outside the owned IPC namespace: ' + path)
    info = os.lstat(path)
    expected = stat.S_IFREG if metadata else stat.S_IFSOCK
    if stat.S_IFMT(info.st_mode) != expected or info.st_uid != os.getuid():
        raise RecoveryError('resource has wrong type or owner: ' + path)
    return {'path': path, 'device': info.st_dev, 'inode': info.st_ino,
            'type': stat.S_IFMT(info.st_mode), 'uid': info.st_uid}


class Group:
    """Own direct children from registration through confirmed-dead cleanup.

    Call this API from one thread. Callers may write each process's stdin but must
    not wait/reap or signal it independently. Every GPU holder must belong to this
    group; peers must not fork GPU users or reinitialise their pool after ACK.
    """

    def __init__(self, commands, stage, startup_timeout=10.0, stop_timeout=5.0):
        validate(commands, startup_timeout, stop_timeout)
        self.stage = Path(stage)
        if not self.stage.is_absolute():
            raise ValueError('stage must be absolute')
        self.stage.mkdir(mode=0o700, exist_ok=False)
        self.commands = commands
        self.startup_timeout = startup_timeout
        self.stop_timeout = stop_timeout
        self.processes = []
        self.epoch_dir = None
        self._pidfds = []
        self._streams = []
        self._pins = []
        self._resources = []
        self._registered = False
        self._failed = False
        self._stopped = True
        self._evidence = {'version': 1, 'epochs': []}

    def _save(self):
        temporary = self.stage / 'recovery.json.tmp'
        temporary.write_text(json.dumps(self._evidence, indent=2) + '\n')
        temporary.replace(self.stage / 'recovery.json')

    def _pin_resource(self, path, pid):
        resource = _resource(path, pid)
        pin = os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
        self._pins.append(pin)
        self._epoch['resource_pins'] = len(self._pins)
        info = os.fstat(pin)
        pinned = (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_uid)
        expected = tuple(resource[key] for key in ('device', 'inode', 'type', 'uid'))
        if pinned != expected or _resource(path, pid) != resource:
            raise RecoveryError('resource changed while registering: ' + path)
        return resource

    def start(self):
        """Start and acknowledge an epoch only after every child registers."""
        if self._failed or not self._stopped:
            raise RecoveryError('group is failed or already running')
        self.epoch_dir = self.stage / f'epoch-{len(self._evidence["epochs"])}'
        self.epoch_dir.mkdir(mode=0o700)
        self.processes, self._pidfds, self._streams, self._resources = [], [], [], []
        self._registered, self._stopped = False, False
        self._epoch = {'directory': str(self.epoch_dir), 'peers': [], 'registered': False,
                       'session_id': str(uuid.uuid4())}
        self._evidence['epochs'].append(self._epoch)
        self._save()
        try:
            resolved = [command_argv(command) for command in self.commands]
            for index, (command, argv) in enumerate(zip(self.commands, resolved)):
                manifest = self.epoch_dir / f'peer-{index}.json'
                ack = self.epoch_dir / f'peer-{index}.ack'
                env = os.environ.copy()
                env.update(command.get('env', {}))
                env.update(G3_RECOVERY_MANIFEST=str(manifest), G3_RECOVERY_ACK=str(ack),
                           G3_RECOVERY_START_TIMEOUT=str(self.startup_timeout),
                           G3_RECOVERY_ROLE=command.get('role', ''),
                           HERMOS_SESSION_ID=self._epoch['session_id'])
                stdout = (self.epoch_dir / f'peer-{index}.stdout').open('w')
                self._streams.append(stdout)
                stderr = (self.epoch_dir / f'peer-{index}.stderr').open('w')
                self._streams.append(stderr)
                parent_pid = os.getpid()
                child = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE,
                                         stdout=stdout, stderr=stderr, text=True,
                                         preexec_fn=lambda: _die_with_parent(parent_pid))
                self.processes.append(child)
                self._pidfds.append(None)
                entry = {'pid': child.pid, 'argv': argv, 'manifest': str(manifest),
                         'id': command.get('id', str(index)), 'role': command.get('role')}
                self._epoch['peers'].append(entry)
                try:
                    self._pidfds[-1] = os.pidfd_open(child.pid)
                except OSError as error:
                    # This direct child has not been polled/reaped by anyone.
                    # Its PID cannot be reused until this sole owner waits.
                    entry['pidfd_open_error'] = repr(error)
                    child.kill()
                    child.wait(timeout=self.stop_timeout)
                    entry['startup_kill_without_pidfd'] = True
                    raise
            deadline = time.monotonic() + self.startup_timeout
            pending = set(range(len(self.processes)))
            while pending:
                for index, child in enumerate(self.processes):
                    if child.poll() is not None:
                        raise RecoveryError(f'peer {index} exited before registration ACK')
                    if index not in pending:
                        continue
                    entry = self._epoch['peers'][index]
                    manifest = Path(entry['manifest'])
                    if not manifest.exists():
                        continue
                    info = manifest.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                        raise RecoveryError('registration manifest has wrong type or owner')
                    registration = json.loads(manifest.read_text())
                    if (registration.get('version') != 1 or registration.get('pid') != child.pid or
                            not isinstance(registration.get('resources'), list)):
                        raise RecoveryError('registration version, pid or resources is invalid')
                    for namespace in ('pid', 'mnt', 'ipc'):
                        own = os.stat(f'/proc/self/ns/{namespace}')
                        peer = os.stat(f'/proc/{child.pid}/ns/{namespace}')
                        if (own.st_dev, own.st_ino) != (peer.st_dev, peer.st_ino):
                            raise RecoveryError('peer is in a different ' + namespace + ' namespace')
                        entry.setdefault('namespaces', {})[namespace] = {
                            'device': peer.st_dev, 'inode': peer.st_ino}
                    entry['resources'] = []
                    for path in registration['resources']:
                        if any(item['path'] == path for item in self._resources):
                            raise RecoveryError('resource registered more than once')
                        resource = self._pin_resource(path, child.pid)
                        self._resources.append(resource)
                        entry['resources'].append(resource)
                    pending.remove(index)
                if pending:
                    if time.monotonic() >= deadline:
                        raise RecoveryError('registration deadline expired')
                    time.sleep(0.01)
            self._registered = self._epoch['registered'] = True
            self._save()
            for index in range(len(self.processes)):
                (self.epoch_dir / f'peer-{index}.ack').touch(exist_ok=False)
        except BaseException as error:
            self._failed = True
            self._epoch['startup_error'] = repr(error)
            try:
                self.stop(reason='startup failure')
            except Exception as stop_error:
                self._epoch['stop_error'] = repr(stop_error)
                self._save()
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise RecoveryError('startup failed: ' + str(error)) from error
        return self

    def _signal(self, signum):
        errors = []
        for index, (child, pidfd) in enumerate(zip(self.processes, self._pidfds)):
            if child.poll() is not None:
                continue
            try:
                if pidfd is None:
                    raise RecoveryError('live child has no pidfd')
                signal.pidfd_send_signal(pidfd, signum)
                self._epoch['peers'][index].setdefault('signals', []).append(signum)
            except ProcessLookupError:
                pass
            except Exception as error:
                errors.append(repr(error))
        return errors

    def _wait(self, deadline):
        for child in self.processes:
            try:
                child.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        return all(child.poll() is not None for child in self.processes)

    def _clean_resources(self):
        if not self._registered or self._failed or any(p.poll() is None for p in self.processes):
            raise RecoveryError('cleanup requires a registered, confirmed-dead complete group')
        # Verify the whole inventory before deleting any name.
        existing = []
        for resource in self._resources:
            try:
                info = os.lstat(resource['path'])
            except FileNotFoundError:
                continue
            current = (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_uid)
            expected = tuple(resource[key] for key in ('device', 'inode', 'type', 'uid'))
            if current != expected:
                raise RecoveryError('registered resource was replaced: ' + resource['path'])
            existing.append(resource['path'])
        self._epoch['removed_resources'] = []
        for path in existing:
            os.unlink(path)
            self._epoch['removed_resources'].append(path)

    def stop(self, reason='stop'):
        """Stop and clean the group, returning its epoch evidence on success.

        Handle-close failures still allow confirmed-dead IPC cleanup, but make
        this group permanently ineligible for restart.
        """
        if self._stopped:
            return getattr(self, '_epoch', None)
        self._epoch['stop_reason'] = reason
        errors = self._signal(signal.SIGTERM)
        settled = self._wait(time.monotonic() + self.stop_timeout)
        if not settled:
            errors.extend(self._signal(signal.SIGKILL))
            settled = self._wait(time.monotonic() + self.stop_timeout)
        self._epoch['returncodes'] = [child.poll() for child in self.processes]
        self._epoch['confirmed_dead'] = settled
        if not settled:
            self._failed = True
            self._epoch['stop_errors'] = errors or ['kill/wait deadline expired']
            self._save()
            raise RecoveryError('group termination could not be confirmed')
        self._stopped = True
        close_errors = []

        def close_handle(label, close):
            try:
                close()
                return True
            except Exception as error:
                close_errors.append(f'{label}: {error!r}')
                return False

        try:
            for pidfd in self._pidfds:
                if pidfd is not None:
                    close_handle('pidfd', lambda fd=pidfd: os.close(fd))
            self._pidfds = []
            for index, child in enumerate(self.processes):
                close_handle(f'peer {index} stdin', child.stdin.close)
            for stream in self._streams:
                close_handle(str(stream.name), stream.close)
            self._streams = []
            if errors:
                self._epoch['stop_errors'] = errors
                raise RecoveryError('group signalling failed')
            if self._registered and not self._failed:
                self._clean_resources()
                self._epoch['cleanup'] = 'complete'
            else:
                self._epoch['cleanup'] = 'refused: incomplete or failed registration'
        except Exception as error:
            self._failed = True
            self._epoch['cleanup'] = 'refused'
            self._epoch['cleanup_error'] = repr(error)
            raise RecoveryError('resource cleanup failed: ' + str(error)) from error
        finally:
            # Pins prevent inode reuse even when a normal peer unlinks its names.
            # Release only after every peer is dead and cleanup was attempted.
            self._epoch['resource_pins_released'] = 0
            for pin in self._pins:
                if close_handle('resource pin', lambda fd=pin: os.close(fd)):
                    self._epoch['resource_pins_released'] += 1
            self._pins = []
            if close_errors:
                self._failed = True
                self._epoch['close_errors'] = close_errors
            self._save()
        if close_errors:
            raise RecoveryError('owned handle close failed; restart is forbidden')
        return self._epoch

    def restart(self, reason='recover'):
        """Replace the whole epoch only after successful stop and cleanup."""
        if self._failed:
            raise RecoveryError('failed ownership or cleanup forbids restart')
        self.stop(reason)
        return self.start()
