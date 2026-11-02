"""Read Linux UDP counters without changing socket or kernel configuration."""

import os
from pathlib import Path


def udp_counters(text):
    """Decode the paired /proc/net/snmp UDP header and counter rows."""
    rows = [line.split()[1:] for line in text.splitlines() if line.startswith('Udp:')]
    if len(rows) != 2 or not rows[0] or len(rows[0]) != len(rows[1]):
        raise ValueError('incomplete UDP counter rows')
    return dict(zip(rows[0], map(int, rows[1])))


def udp_sockets(text, owned_inodes):
    """Filter Linux /proc/net/udp{,6} rows to this peer's open socket FDs."""
    lines = text.splitlines()
    if not lines or lines[0].split()[-1] != 'drops':
        raise ValueError('unsupported UDP socket counter layout')
    result = []
    for line in lines[1:]:
        fields = line.split()
        if len(fields) != 13:
            raise ValueError('incomplete UDP socket counter row')
        if fields[9] in owned_inodes:
            tx, rx = fields[4].split(':')
            result.append(dict(inode=int(fields[9]), local_address=fields[1],
                               remote_address=fields[2], tx_queue_bytes=int(tx, 16),
                               rx_queue_bytes=int(rx, 16), drops=int(fields[-1])))
    return result


def owned_udp_sockets():
    inodes = set()
    for path in Path('/proc/self/fd').iterdir():
        try:
            target = os.readlink(path)
        except FileNotFoundError:
            # A runtime worker may close a descriptor during this observation.
            continue
        if target.startswith('socket:['):
            inodes.add(target[8:-1])
    return {family: udp_sockets(Path(f'/proc/net/{family}').read_text(), inodes)
            for family in ('udp', 'udp6')}


def network_snapshot():
    """These counters cover the whole network namespace, including other traffic."""
    settings = {name: int(Path('/proc/sys/net/core', name).read_text()) for name in
                ('rmem_default', 'rmem_max', 'wmem_default', 'wmem_max')}
    return dict(udp=udp_counters(Path('/proc/net/snmp').read_text()), socket_limits=settings,
                namespace=os.readlink('/proc/self/ns/net'))
