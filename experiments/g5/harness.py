"""Unmeasured process control and evidence persistence shared by G5 scenarios."""

import json
import os
import random
import time


class Events:
    """Read complete peer control lines, retaining an unfinished trailing line."""

    def __init__(self, epoch, peers):
        self.epoch = epoch
        self.offsets = [0] * peers

    def receive(self, index, expected, timeout=20):
        deadline = time.monotonic() + timeout
        path = self.epoch.epoch_dir / f'peer-{index}.stdout'
        while time.monotonic() < deadline:
            for line in path.read_text().split('\n')[self.offsets[index]:-1]:
                self.offsets[index] += 1
                if not line.startswith('{'):
                    continue
                value = json.loads(line)
                if value.get('event') != expected:
                    raise RuntimeError(f'expected {expected}, got {value}')
                return value
            if self.epoch.poll() is not None:
                raise RuntimeError(f'peer exited before {expected}; inspect {path.parent}')
            time.sleep(0.005)
        raise TimeoutError(f'{expected}; inspect {path}')


def verify_teardown(record, peers):
    """Require exact integer-zero peer/descendant exits and all worker-death evidence."""
    codes, descendants = record.get('returncodes'), record.get('descendants')
    if not (all(record.get(key) is True for key in (
            'verified_teardown', 'teardown_complete', 'echild')) and
            record.get('peer_failed') is False and
            isinstance(codes, list) and len(codes) == peers and
            all(type(code) is int and code == 0 for code in codes) and
            isinstance(descendants, list) and
            all(isinstance(item, dict) and type(item.get('returncode')) is int and
                item['returncode'] == 0 for item in descendants)):
        raise RuntimeError(f'{peers}-peer benchmark teardown failed: {record}')


def peer_environment(rmw, *, hermos, heaphook):
    """Use the same private device and bounded thread settings in every scenario."""
    environment = dict(HERMOS_DEVICE_PATH='/dev/hermos_rclpy', HERMOS_BRIDGE_MODE='off',
                       HERMOS_NO_DISCOVERY_AGENT='1', RMW_IMPLEMENTATION=rmw,
                       ROS_DOMAIN_ID=str(140 + os.getpid() % 60), ROS_LOCALHOST_ONLY='1',
                       OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', PYTHONDONTWRITEBYTECODE='1')
    if hermos:
        environment['LD_PRELOAD'] = str(heaphook)
    return environment


def wait_connected(events, indices):
    """Prove discovery for each receiving edge before starting publication."""
    for index in indices:
        deadline = time.monotonic() + 15
        while True:
            events.epoch.send(index, 'connections')
            if events.receive(index, 'CONNECTED')['count']:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f'endpoint discovery for peer {index}')
            time.sleep(0.05)
    time.sleep(0.2)


def wait_received(events, index, expected):
    """Allow bounded delivery drain after sends; missing receipts remain explicit loss."""
    deadline = time.monotonic() + 3
    while True:
        events.epoch.send(index, 'count')
        count = events.receive(index, 'COUNT')['count']
        if count >= expected or time.monotonic() >= deadline:
            return count
        time.sleep(0.02)


def execute_epoch(commands, path, *, expected, period_ns, startup_timeout):
    """Run a source followed by receiving peers and persist teardown even on failure.

    Each scenario supplies commands and owns its payload and timestamp code. This
    control path runs outside measurement: discovery, start, bounded drain, save,
    then supervisor teardown. All peer reports are saved before any peer exits.
    """
    from isolated import IsolatedEpoch
    peers = len(commands)
    epoch = IsolatedEpoch(commands, path / 'epoch',
                          startup_timeout=startup_timeout, stop_timeout=5)
    epoch.start()
    try:
        events = Events(epoch, peers)
        for index in range(peers):
            events.receive(index, 'READY')
        wait_connected(events, range(1, peers))
        epoch.send(0, 'start')
        sent = events.receive(0, 'SENT', timeout=expected * period_ns / 1e9 + 30)
        if sent['count'] != expected:
            raise RuntimeError(f'incomplete publication: {sent}')
        wait_received(events, peers - 1, expected)
        for index in range(peers):
            epoch.send(index, 'finish')
            events.receive(index, 'SAVED')
    finally:
        evidence = epoch.stop()
        (path / 'teardown.json').write_text(json.dumps(evidence, indent=2) + '\n')
    verify_teardown(evidence, peers)


def run_matrix(output, protocol, matrix, *, capture, archive_sources, run_case, report,
               shuffle=True):
    """Freeze inputs before execution; continue on loss, fail immediately on invalid evidence.

    Receipt loss (including zero receipts) produces partial reports and a final
    summary, then exit 2 after the remaining cases. Protocol/process errors raise
    immediately. The output must be fresh, and source/artifact identity must stay
    fixed throughout the run.
    """
    output.mkdir(exist_ok=False)
    matrix = list(matrix)
    if shuffle:
        random.Random(protocol['order_seed']).shuffle(matrix)
    identity = capture()
    (output / 'provenance.json').write_text(json.dumps(identity, indent=2) + '\n')
    archive_sources(identity, output / 'sources.tar.gz')
    (output / 'protocol.json').write_text(
        json.dumps(dict(protocol=protocol, cases=matrix), indent=2) + '\n')
    results = []
    for number, case in enumerate(matrix):
        print(f"{number+1}/{len(matrix)} {case['name']}", flush=True)
        result = run_case(case, protocol, output / case['name'])
        results.append(result)
        report(results, output, protocol)
        if not result['complete']:
            delivery = result.get('source_to_sink', result)
            relay = (f"; {result['missing_relay_records']} missing relay rows"
                     if 'missing_relay_records' in result else '')
            print(f"LOSS: {delivery['lost']} measured messages{relay}; comparison invalid",
                  flush=True)
    if capture() != identity:
        raise RuntimeError('source or production artifacts changed during measurement')
    complete = all(result['complete'] for result in results)
    (output / 'summary.json').write_text(json.dumps(
        dict(protocol=protocol, results=results, all_messages_received=complete),
        indent=2) + '\n')
    if not complete:
        raise SystemExit(2)
