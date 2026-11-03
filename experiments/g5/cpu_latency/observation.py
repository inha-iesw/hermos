"""Read Linux resource observations without changing kernel configuration."""

import os
from pathlib import Path
import re


def smaps_memory(text):
    """Separate process residency from Hermos heap mappings, in kernel KiB units.

    Shared heap RSS must not be summed across peers as physical usage. PSS
    apportions shared pages; an absent kernel field is unavailable, not zero.
    """
    mappings = []
    current = None
    for line in text.splitlines():
        if re.match(r'^[0-9a-fA-F]+-[0-9a-fA-F]+ ', line):
            fields = line.split(maxsplit=5)
            current = dict(shared_heap=len(fields) == 6 and
                           fields[5].startswith('/dev/shm/hermos@'))
            mappings.append(current)
        elif current is not None and line.startswith(('Rss:', 'Pss:')):
            key, value, unit = line.split()
            if unit != 'kB':
                raise ValueError('unsupported smaps memory unit')
            current[key[:-1].lower() + '_kib'] = int(value)

    def totals(items):
        return {key: (sum(item[key] for item in items)
                      if mappings and all(key in item for item in items) else None)
                for key in ('rss_kib', 'pss_kib')}

    shared = [item for item in mappings if item['shared_heap']]
    return dict(process=totals(mappings),
                shared_heap=dict(mapping_count=len(shared) if mappings else None,
                                 **totals(shared)))


def memory_snapshot():
    """Observe this process and its mounted shm filesystem, not summed peer RSS."""
    result = {}
    try:
        result['smaps'] = smaps_memory(Path('/proc/self/smaps').read_text())
    except (OSError, ValueError) as error:
        result.update(smaps=None, smaps_error=str(error))
    try:
        usage = os.statvfs('/dev/shm')
        result['shm'] = dict(total_bytes=usage.f_blocks * usage.f_frsize,
                             used_bytes=(usage.f_blocks - usage.f_bfree) * usage.f_frsize,
                             mount_namespace=os.readlink('/proc/self/ns/mnt'))
    except OSError as error:
        result.update(shm=None, shm_error=str(error))
    return result


def thread_snapshot():
    """Read every currently visible thread's own placement and scheduling policy."""
    result = []
    for directory in sorted(Path('/proc/self/task').iterdir(), key=lambda path: int(path.name)):
        tid = int(directory.name)
        record = dict(tid=tid)
        try:
            record.update(name=(directory / 'comm').read_text().strip(),
                          cpu_affinity=sorted(os.sched_getaffinity(tid)),
                          scheduler=os.sched_getscheduler(tid),
                          priority=os.sched_getparam(tid).sched_priority)
        except OSError as error:
            # Threads can exit during this non-atomic snapshot.
            record.update(name=None, cpu_affinity=None, scheduler=None, priority=None,
                          error=str(error))
        result.append(record)
    return result


def cpu_frequency_snapshot():
    """Capture advertised cpufreq state; unavailable policies/fields stay null."""
    policies = sorted(Path('/sys/devices/system/cpu/cpufreq').glob('policy*'))
    result = []
    for policy in policies:
        record = dict(policy=policy.name)
        for name in ('related_cpus', 'scaling_governor', 'scaling_cur_freq',
                     'scaling_min_freq', 'scaling_max_freq'):
            frequency = name.endswith('_freq')
            key = name + '_khz' if frequency else name
            try:
                value = (policy / name).read_text().strip()
                record[key] = int(value) if frequency else value
            except (OSError, ValueError):
                record[key] = None
        result.append(record)
    return result or None


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
