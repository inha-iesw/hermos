"""Own one fresh namespace worker and validate its complete teardown."""

import json
import os
from pathlib import Path
import select
import stat
import subprocess
import time

from .group import RecoveryError, validate
from .control import write_pipe
from .installation import validate_installation


BOOTSTRAP = Path('/usr/local/libexec/rclpy-zc-gpu/namespace-bootstrap.py')
REQUIRED_ENVIRONMENT = {'HERMOS_BRIDGE_MODE': 'off', 'HERMOS_NO_DISCOVERY_AGENT': '1'}


def installed_prefix():
    """Reject mixed runtime origins before choosing bootstrap and worker files."""
    return validate_installation()


def trusted_bootstrap():
    """Reject writable/symlinked installation paths before invoking sudo."""
    for path in (BOOTSTRAP, *BOOTSTRAP.parents):
        info = path.lstat()
        expected_type = stat.S_ISREG if path == BOOTSTRAP else stat.S_ISDIR
        if info.st_uid != 0 or info.st_mode & 0o022 or not expected_type(info.st_mode):
            raise RecoveryError('bootstrap and its ancestors must be root-owned and not writable: ' + str(path))
    source = installed_prefix() / 'share/hermos_runtime/system/namespace-bootstrap.py'
    if BOOTSTRAP.read_bytes() != source.read_bytes():
        raise RecoveryError('installed bootstrap differs from this source; administrator must update it')
    return str(BOOTSTRAP)


class IsolatedEpoch:
    """Single-owner API; restart by constructing a new epoch with a new stage.

    Peers remain direct children of the worker. The caller may send stdin lines
    and inspect peer logs, but must not wait or reap peers or the worker itself.
    All GPU holders and Hermos communication peers must be inside this epoch.
    """

    def __init__(self, commands, stage, startup_timeout=10.0, stop_timeout=5.0):
        validate(commands, startup_timeout, stop_timeout)
        if os.getuid() == 0 or os.geteuid() != os.getuid():
            raise ValueError('run the supervisor as a non-root user with sudo access')
        if os.environ.get('LD_PRELOAD'):
            raise ValueError('put LD_PRELOAD in each peer environment, not the supervisor')
        for command in commands:
            environment = {**os.environ, **command.get('env', {})}
            if any(environment.get(key) != value for key, value in REQUIRED_ENVIRONMENT.items()):
                raise ValueError('isolated peers require HERMOS_BRIDGE_MODE=off and HERMOS_NO_DISCOVERY_AGENT=1')
        self.stage = Path(stage)
        if not self.stage.is_absolute() or self.stage.resolve().is_relative_to('/dev/shm'):
            raise ValueError('stage must be absolute and outside /dev/shm')
        if Path.cwd().resolve().is_relative_to('/dev/shm'):
            raise ValueError('supervisor working directory must be outside /dev/shm')
        self.stage.mkdir(mode=0o700, exist_ok=False)
        self.commands = commands
        self.startup_timeout, self.stop_timeout = startup_timeout, stop_timeout
        self.process = None
        self.worker_pidfd = None
        self.peers = []
        self.result = None
        self._startup_failed = False
        self._wait_error = None
        self._worker_control_error = None
        self._peer_start_times = {}
        self._streams = []

    def _send(self, value):
        write_pipe(self.process.stdin.fileno(), (json.dumps(value) + '\n').encode(), self.stop_timeout)

    def _wait_file(self, name, deadline):
        path = self.stage / name
        while not path.exists():
            if self.process.poll() is not None:
                raise RecoveryError('worker exited before ' + name + '; inspect ' + str(self.stage))
            if time.monotonic() >= deadline:
                raise TimeoutError('worker startup deadline expired: ' + name)
            time.sleep(0.01)
        return json.loads(path.read_text())

    def start(self):
        """Acknowledge the actual worker identity, then wait for peer registration."""
        if self.process is not None:
            raise RecoveryError('an isolated epoch can only start once')
        bootstrap = trusted_bootstrap()
        for name in ('stdout', 'stderr'):
            self._streams.append((self.stage / ('worker.' + name)).open('w'))
        try:
            self.process = subprocess.Popen(
                ['sudo', '-n', '/usr/bin/python3', '-I', '-B', bootstrap,
                 str(installed_prefix() / 'lib/hermos_runtime/namespace_worker.py'), str(self.stage)],
                stdin=subprocess.PIPE, stdout=self._streams[0], stderr=self._streams[1],
                text=True, cwd='/', env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
                                'PYTHONDONTWRITEBYTECODE': '1'})
            (self.stage / 'launcher.json').write_text(json.dumps({'wrapper_pid': self.process.pid}) + '\n')
            self._send({'version': 1, 'commands': self.commands, 'environment': dict(os.environ),
                        'cwd': str(Path.cwd()),
                        'startup_timeout': self.startup_timeout, 'stop_timeout': self.stop_timeout})
            deadline = time.monotonic() + self.startup_timeout
            worker = self._wait_file('worker.json', deadline)
            pid = worker['pid']
            if type(pid) is not int or pid <= 0:
                raise RecoveryError('invalid worker identity')
            self.worker_pidfd = os.pidfd_open(pid)
            # The worker cannot create peers or exit normally until this ACK.
            # Pin first, and verify that it is still the live announced process.
            if select.select([self.worker_pidfd], [], [], 0)[0]:
                raise RecoveryError('worker died before acknowledgement')
            if Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19] != worker['start_time']:
                raise RecoveryError('worker PID was reused before acknowledgement')
            self.worker_pid = pid
            identity = Path(f'/proc/{pid}/status').read_text()
            uid_line = next(line for line in identity.splitlines() if line.startswith('Uid:'))
            if any(int(value) != os.getuid() for value in uid_line.split()[1:]):
                raise RecoveryError('worker did not drop to the supervisor user')
            self._send({'ack': True})
            ready = self._wait_file('ready.json', deadline)
            self.peers = ready['peers']
            self._peer_start_times = ready.get('peer_start_times', {})
            self.epoch_dir = Path(ready['epoch_dir'])
            return self
        except BaseException:
            self._startup_failed = True
            if self.process is not None:
                try:
                    self.stop()
                except Exception:
                    pass  # The original startup error wins; worker logs remain.
            else:
                self._close_handles()
            raise

    def send(self, peer, line):
        """Write one command line to a registered peer; replies remain in its log."""
        if not self.peers or self.result is not None:
            raise RecoveryError('epoch is not running')
        self._send({'peer': peer, 'stdin': line})

    def poll(self):
        """Return the sudo wrapper's status, without treating it as worker proof."""
        return self.process.poll()

    def _close_handles(self):
        errors = []
        handles = self._streams
        self._streams = []
        if self.process is not None:
            handles.append(self.process.stdin)
        for stream in handles:
            try:
                stream.close()
            except OSError as error:
                errors.append(repr(error))
        if self.worker_pidfd is not None:
            os.close(self.worker_pidfd)
            self.worker_pidfd = None
        if errors:
            raise RecoveryError('worker handle close failed: ' + str(errors))

    def _teardown_budget(self):
        # Startup/registration, pending peer write, TERM, KILL, descendant drain.
        return self.startup_timeout + 4 * self.stop_timeout + 2

    def _peer_states(self):
        states = {}
        for pid in self.peers:
            try:
                fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
                expected = self._peer_start_times.get(str(pid))
                states[str(pid)] = ('identity_unknown' if expected is None else
                                    'pid_reused' if fields[19] != expected else
                                    'dead' if fields[0] in ('Z', 'X') else 'alive')
            except FileNotFoundError:
                states[str(pid)] = 'absent'
            except (OSError, IndexError) as error:
                states[str(pid)] = 'unknown: ' + repr(error)
        return states

    def wait(self, timeout=None):
        """Wait for completion; timeout leaves ownership handles available to stop()."""
        if self._wait_error is not None:
            raise self._wait_error
        if self.result is not None:
            if not self.result.get('verified_teardown'):
                raise RecoveryError('worker teardown previously failed')
            return self.result
        # A caller can wait for a long-running epoch without implicitly stopping
        # it. In particular TimeoutExpired must not close stdin or the pidfd.
        code = self.process.wait(timeout=timeout)
        owner = {'verified_teardown': False, 'wrapper_pid': self.process.pid,
                 'worker_pid': getattr(self, 'worker_pid', None)}
        terminal = True
        try:
            owner['wrapper_exit_code'] = code
            if self.worker_pidfd is not None and not select.select([self.worker_pidfd], [], [], 0)[0]:
                terminal = False
                # A sudo wrapper can die while its worker still owns peers.
                # EOF asks that worker to drain; do not signal the wrapper PID.
                try:
                    self.process.stdin.close()
                except (OSError, ValueError) as error:
                    self._worker_control_error = repr(error)
                select.select([self.worker_pidfd], [], [], self._teardown_budget())
            if self.worker_pidfd is None or not select.select([self.worker_pidfd], [], [], 0)[0]:
                terminal = self.worker_pidfd is None
                raise RecoveryError('actual namespace worker death was not confirmed')
            owner['worker_death_confirmed'] = True
            terminal = True
            self.result = json.loads((self.stage / 'worker-result.json').read_text())
            self.result['verified_teardown'] = False
            if self._worker_control_error is not None:
                self.result['retry_allowed'] = False
                raise RecoveryError('worker EOF close failed after drain: ' + self._worker_control_error)
            if self._startup_failed:
                self.result['retry_allowed'] = False
                raise RecoveryError('failed startup cannot be adopted as a successful epoch')
            if (code != 0 or self.result.get('version') != 1 or
                    self.result.get('worker_pid') != self.worker_pid or
                    not self.result.get('teardown_complete') or not self.result.get('echild')):
                raise RecoveryError('worker teardown failed; inspect ' + str(self.stage))
            self.result['verified_teardown'] = True
            owner['verified_teardown'] = True
        except BaseException as error:
            owner['error'] = repr(error)
            if terminal:
                self._wait_error = RecoveryError('worker teardown previously failed: ' + repr(error))
            raise
        finally:
            if self._worker_control_error is not None:
                owner['control_close_error'] = self._worker_control_error
            owner['peer_states'] = self._peer_states()
            try:
                if terminal:
                    self._close_handles()
            except Exception as error:
                owner.update(verified_teardown=False, close_error=repr(error))
                self._wait_error = RecoveryError('worker handle close failed: ' + repr(error))
                if self.result is not None:
                    self.result['verified_teardown'] = False
                raise
            finally:
                (self.stage / 'owner.json').write_text(json.dumps(owner, indent=2) + '\n')
        return self.result

    def stop(self):
        """Request orderly group shutdown and natural daemon exit, then wait."""
        if self.process is None:
            return None
        errors = []
        if self._wait_error is not None:
            raise self._wait_error
        if self.result is None and self.process.poll() is None and not self.process.stdin.closed:
            try:
                self._send({'stop': True})
            except BrokenPipeError:
                pass
            except (OSError, TimeoutError, ValueError) as error:
                errors.append(repr(error))
                # Closing an empty userspace buffer delivers EOF even if the
                # control pipe is full; the worker must still perform cleanup.
                try:
                    self.process.stdin.close()
                except OSError as close_error:
                    errors.append(repr(close_error))
        try:
            result = self.wait(timeout=self._teardown_budget())
        finally:
            if errors:
                if self.result is not None:
                    self.result['retry_allowed'] = False
                    self.result['control_errors'] = errors
                (self.stage / 'control-errors.json').write_text(json.dumps(errors) + '\n')
        if errors:
            raise RecoveryError('stop control failed after cleanup attempt: ' + str(errors))
        return result
