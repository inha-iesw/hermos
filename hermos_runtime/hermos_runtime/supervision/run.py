"""Run a bounded sequence of complete GPU peer epochs."""

import argparse
import ctypes
import json
import os
from pathlib import Path
import signal
import time

from .group import validate_config
from .isolated import IsolatedEpoch


def watch_parent(parent_pid):
    """Request ordinary drain when the direct launch parent dies, closing the race.

    Install the supervisor signal handlers before calling this. No peers exist
    yet, so a parent already gone can fail without creating an orphan epoch.
    """
    if parent_pid <= 1 or os.getppid() != parent_pid:
        raise RuntimeError('launch parent disappeared before supervisor startup')
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), signal.SIGTERM)


def announce(state, **details):
    """Forward lifecycle metadata to launch; registration is not workload readiness."""
    print(json.dumps(dict(version=1, state=state, **details)), flush=True)


def _record_outcome(evidence, result):
    evidence['verified_teardown'] = bool(result and result.get('verified_teardown'))
    evidence['peer_failed'] = result.get('peer_failed', True) if result else True


def main():
    """Run commands from a JSON configuration and preserve termination evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path, help='JSON object containing commands')
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--startup-timeout', type=float, default=10.0)
    parser.add_argument('--stop-timeout', type=float, default=5.0)
    parser.add_argument('--max-restarts', type=int, default=1)
    parser.add_argument('--parent-pid', type=int, help='direct launch parent to watch for death')
    args = parser.parse_args()
    if args.max_restarts < 0:
        parser.error('--max-restarts must be nonnegative')
    config = json.loads(args.config.read_text())
    validate_config(config, args.startup_timeout, args.stop_timeout)
    if not args.stage.is_absolute() or args.stage.resolve().is_relative_to('/dev/shm'):
        parser.error('--stage must be absolute and outside /dev/shm')
    args.stage.mkdir(mode=0o700, exist_ok=False)
    evidence = {'max_restarts': args.max_restarts, 'restarts': 0, 'exit_code': 1,
                'verified_teardown': False, 'peer_failed': False}
    requested_signal = []
    epoch = None

    def interrupted(signum, frame):
        # Do not interrupt an in-progress group cleanup or its evidence write.
        requested_signal.append(signum)

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.parent_pid is not None:
            watch_parent(args.parent_pid)
        while True:
            if requested_signal:
                if epoch is None:
                    evidence['verified_teardown'] = True  # No epoch was created.
                evidence.update(exit_code=128 + requested_signal[0], reason='supervisor signal')
                break
            evidence['verified_teardown'] = False
            evidence['peer_failed'] = True  # Healthy peers require a worker result.
            epoch = None
            epoch = IsolatedEpoch(config['commands'], args.stage / f'epoch-{evidence["restarts"]}',
                                  args.startup_timeout, args.stop_timeout)
            announce('starting', stage=str(epoch.stage), epoch=evidence['restarts'])
            epoch.start()
            announce('registration-ready', epoch=evidence['restarts'], peers=[
                dict(id=command.get('id', str(index)), role=command.get('role'),
                     stdout=str(epoch.epoch_dir / f'peer-{index}.stdout'),
                     stderr=str(epoch.epoch_dir / f'peer-{index}.stderr'))
                for index, command in enumerate(config['commands'])])
            while epoch.poll() is None and not requested_signal:
                time.sleep(0.01)
            if requested_signal:
                result = epoch.stop()
                _record_outcome(evidence, result)
                evidence.update(exit_code=128 + requested_signal[0], reason='supervisor signal')
                break
            result = epoch.wait()
            _record_outcome(evidence, result)
            if requested_signal:
                evidence.update(exit_code=128 + requested_signal[0], reason='supervisor signal')
                break
            if not result.get('verified_teardown') or result.get('requested_stop'):
                raise RuntimeError('epoch did not complete normally')
            if not result['peer_failed']:
                evidence.update(exit_code=0, reason='peer completion stopped the group')
                break
            if not result.get('retry_allowed'):
                raise RuntimeError('worker refused restart')
            if evidence['restarts'] >= args.max_restarts:
                evidence['reason'] = 'restart limit reached'
                break
            evidence['restarts'] += 1
    except Exception as error:
        evidence['error'] = repr(error)
        try:
            if epoch is not None:
                result = epoch.stop()
                _record_outcome(evidence, result)
        except Exception as stop_error:
            evidence['stop_error'] = repr(stop_error)
    finally:
        if epoch is None:
            evidence.update(verified_teardown=True, peer_failed=False)
        if requested_signal:
            evidence['exit_code'] = 128 + requested_signal[0]
        evidence['state'] = 'stopped' if evidence['verified_teardown'] else 'incomplete'
        (args.stage / 'cli.json').write_text(json.dumps(evidence, indent=2) + '\n')
        announce(evidence['state'], stage=str(args.stage), exit_code=evidence['exit_code'],
                 peer_failed=evidence['peer_failed'])
    return evidence['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
