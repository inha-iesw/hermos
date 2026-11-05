#!/usr/bin/env python3
"""Host-only substitute: real supervisor/registration, no private namespace or device."""

import json
import os
from pathlib import Path
import signal
import sys

from hermos_runtime.supervision import run
from hermos_runtime.supervision.group import Group


class HostEpoch:
    def __init__(self, commands, stage, startup_timeout, stop_timeout):
        self.stage = stage
        stage.mkdir()
        self.group = Group(commands, stage / 'group', startup_timeout, stop_timeout)

    def start(self):
        self.group.start()
        self.epoch_dir = self.group.epoch_dir

    def poll(self):
        return 0 if any(peer.poll() is not None for peer in self.group.processes) else None

    def finish(self, requested):
        failed = any(peer.poll() not in (None, 0) for peer in self.group.processes)
        result = self.group.stop()
        failed |= any(peer.returncode != 0 and not (
            peer.returncode == -signal.SIGTERM and
            signal.SIGTERM in result['peers'][index].get('signals', []))
            for index, peer in enumerate(self.group.processes))
        return dict(verified_teardown=result['cleanup'] == 'complete', requested_stop=requested,
                    peer_failed=failed, retry_allowed=failed and not requested)

    def stop(self):
        return self.finish(True)

    def wait(self):
        return self.finish(False)


if __name__ == '__main__':
    mode = os.environ.get('HOST_SUPERVISOR_MODE')
    if mode:
        stage = Path(sys.argv[sys.argv.index('--stage') + 1])
        stage.mkdir()
        code = {'failure': 70, 'signal': 143}.get(mode, 0)
        evidence = dict(state='stopped', verified_teardown=True, peer_failed=False, exit_code=code)
        if mode == 'incomplete':
            evidence['verified_teardown'] = False
        elif mode == 'state':
            evidence['state'] = 'incomplete'
        elif mode == 'code':
            evidence['exit_code'] = 70
        elif mode == 'peer':
            evidence['peer_failed'] = True
        if mode != 'missing':
            (stage / 'cli.json').write_text(json.dumps(evidence))
        sys.exit(code)
    run.IsolatedEpoch = HostEpoch
    sys.exit(run.main())
