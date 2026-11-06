"""Run the backend metadata agent as one directly registered observation peer."""

import argparse
import os

from ..cpu import registered as factory_runner
from ..environment import resolve_installation
from ..environment.process import check_device


def main(argv=None):
    """Observe only after ACK, using the shared context and shutdown contract.

    The backend supplies a normal context-owned Node factory and never depends
    on hermos_runtime. This entry requires a supervisor-prepared environment;
    it must not load the payload allocator or enable bridge dispatch.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    _, arguments = parser.parse_known_args(argv)
    if os.environ.get('LD_PRELOAD'):
        raise RuntimeError('metadata peer requires an empty LD_PRELOAD')
    for name, expected in (('G3_RECOVERY_ROLE', 'metadata'), ('HERMOS_BRIDGE_MODE', 'off'),
                           ('HERMOS_NO_DISCOVERY_AGENT', '1')):
        if os.environ.get(name) != expected:
            raise RuntimeError(f'metadata peer requires {name}={expected}')
    installation = resolve_installation()
    device = str(installation.device)
    if os.environ.get('HERMOS_DEVICE_PATH', device) != device:
        raise RuntimeError('metadata peer HERMOS_DEVICE_PATH conflicts with the installation')
    check_device(installation)
    os.environ['HERMOS_DEVICE_PATH'] = device
    return factory_runner.main(['ros2hermos_discovery_agent.observation:create', *arguments])
