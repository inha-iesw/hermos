"""Run a bounded sequence of complete GPU peer epochs."""

import argparse
import json
from pathlib import Path
import signal
import time

from .group import validate_config
from .isolated import IsolatedEpoch


def main():
    """Run commands from a JSON configuration and preserve termination evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path, help='JSON object containing commands')
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--startup-timeout', type=float, default=10.0)
    parser.add_argument('--stop-timeout', type=float, default=5.0)
    parser.add_argument('--max-restarts', type=int, default=1)
    args = parser.parse_args()
    if args.max_restarts < 0:
        parser.error('--max-restarts must be nonnegative')
    config = json.loads(args.config.read_text())
    validate_config(config, args.startup_timeout, args.stop_timeout)
    if not args.stage.is_absolute() or args.stage.resolve().is_relative_to('/dev/shm'):
        parser.error('--stage must be absolute and outside /dev/shm')
    args.stage.mkdir(mode=0o700, exist_ok=False)
    evidence = {'max_restarts': args.max_restarts, 'restarts': 0, 'exit_code': 1}
    requested_signal = []
    epoch = None

    def interrupted(signum, frame):
        # Do not interrupt an in-progress group cleanup or its evidence write.
        requested_signal.append(signum)

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        while True:
            if requested_signal:
                evidence.update(exit_code=128 + requested_signal[0], reason='supervisor signal')
                break
            epoch = IsolatedEpoch(config['commands'], args.stage / f'epoch-{evidence["restarts"]}',
                                  args.startup_timeout, args.stop_timeout)
            epoch.start()
            while epoch.poll() is None and not requested_signal:
                time.sleep(0.01)
            if requested_signal:
                epoch.stop()
                evidence.update(exit_code=128 + requested_signal[0], reason='supervisor signal')
                break
            result = epoch.wait()
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
                epoch.stop()
        except Exception as stop_error:
            evidence['stop_error'] = repr(stop_error)
    finally:
        if requested_signal:
            evidence['exit_code'] = 128 + requested_signal[0]
        (args.stage / 'cli.json').write_text(json.dumps(evidence, indent=2) + '\n')
    return evidence['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
