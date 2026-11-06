"""Opt-in real kernel counters and metadata across private IPC/mount namespaces."""

import json
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid

import pytest


pytestmark = pytest.mark.skipif(os.environ.get('HERMOS_E3_RUN_DEVICE') != '1',
    reason='requires a fresh verified E3 kernel/wrapper device bundle')
SCENARIOS = Path(__file__).with_name('scenarios')
ROOT = Path(__file__).resolve().parents[3]
TOPIC = '/e3_observation/images'


def wait_for(read, predicate=bool, timeout=15):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = read()
        if predicate(last):
            return last
        time.sleep(0.02)
    raise AssertionError(f'observation deadline expired; last result: {last!r}')


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else None


def command(directory, role, action, **values):
    identifier = uuid.uuid4().hex
    path = directory / (role + '-command.json')
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(dict(id=identifier, action=action, **values)))
    temporary.replace(path)
    reply = wait_for(lambda: read_json(directory / (role + '-reply.json')),
                     lambda value: value is not None and value['id'] == identifier)
    return reply['result']


@pytest.fixture
def environment():
    from hermos_runtime.environment import resolve_installation

    settings = os.environ.copy()
    settings.pop('LD_PRELOAD', None)
    installation = resolve_installation(providers=['rclpy_hermos_adapters'])
    settings.update(HERMOS_BRIDGE_MODE='off', HERMOS_NO_DISCOVERY_AGENT='1',
                    HERMOS_DEVICE_PATH=str(installation.device),
                    ROS_DOMAIN_ID=str(180 + os.getpid() % 30),
                    RMW_IMPLEMENTATION='rmw_fastrtps_cpp',
                    FASTRTPS_DEFAULT_PROFILES_FILE=str(
                        ROOT / 'rclpy_hermos_gpu/config/dds/fastdds_udp.xml'),
                    PYTHONDONTWRITEBYTECODE='1')
    return settings


@pytest.fixture
def epochs(tmp_path, environment):
    from hermos_runtime.environment import peer_environment, resolve_installation
    from hermos_runtime.supervision.isolated import IsolatedEpoch

    active = []

    def start(roles=('writer', 'reader'), *, domain=None, duplicate=False):
        directory = tmp_path / ('case-' + str(len(active)))
        directory.mkdir()
        settings = dict(environment)
        if domain is not None:
            settings['ROS_DOMAIN_ID'] = str(domain)
        payload = peer_environment(resolve_installation(providers=['rclpy_hermos_adapters']), settings)
        payload['PYTHONPATH'] = str(SCENARIOS) + os.pathsep + payload.get('PYTHONPATH', '')
        commands = [dict(id='observer', role='metadata', package='hermos_runtime',
                         argv=['hermos_metadata_peer'], env=settings)]
        for role in roles:
            commands.append(dict(id=role, role='cpu', package='hermos_runtime',
                argv=['hermos_cpu_peer', 'e3_peer:create', role, str(directory), TOPIC], env=payload))
        if duplicate:
            commands.append(dict(commands[0], id='duplicate'))
        epoch = IsolatedEpoch(commands, directory / 'epoch', startup_timeout=20, stop_timeout=5)
        active.append(epoch)
        epoch.start()
        for role in roles:
            wait_for(lambda: read_json(directory / (role + '-ready.json')))
        return epoch, directory, settings

    yield start
    for epoch in reversed(active):
        epoch.stop()


def query(environment):
    result = subprocess.run(['/usr/bin/python3', str(SCENARIOS / 'query.py'), '4'],
        env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    line = next((line for line in result.stdout.splitlines() if line.startswith('E3_SNAPSHOTS ')), None)
    assert line is not None, result.stdout + result.stderr
    return json.loads(line.removeprefix('E3_SNAPSHOTS '))


def topic(state):
    return next((item for item in state['topics'] if item['topic_name'] == TOPIC), None)


def state_for(states, session):
    return next((state for state in states if state['session_id'] == session), None)


def counters(state):
    item = topic(state)
    return [(entry['endpoint_id'], entry['published_total'], entry['received_total'],
             entry['dropped_history_total'], entry['queued_entries'], entry['retained_entries'])
            for entry in [*item['publishers'], *item['subscribers']]]


def test_external_observer_counts_transfers_and_history_loss_without_payload_borrows(epochs):
    epoch, directory, settings = epochs()
    writer = read_json(directory / 'writer-ready.json')
    reader = read_json(directory / 'reader-ready.json')
    observer_pid = epoch.peers[0]
    manifest = read_json(epoch.epoch_dir / 'peer-0.json')
    assert manifest == dict(version=1, pid=observer_pid, resources=[])
    maps = Path(f'/proc/{observer_pid}/maps').read_text()
    assert 'libhermos_ioctl_wrapper.so' in maps
    assert not any(name in maps for name in ('libhermos.so', 'libhermos_heaphook', 'libcuda', 'libtorch'))
    bundle = Path(os.environ['RCLPY_HERMOS_KMOD']).resolve().parents[1]
    expected = hashlib.sha256((bundle / 'lib/libhermos_ioctl_wrapper.so').read_bytes()).digest()
    wrappers = {Path(line.split(maxsplit=5)[5]) for line in maps.splitlines()
                if line.split(maxsplit=5)[-1].endswith('/libhermos_ioctl_wrapper.so')}
    assert wrappers and all(hashlib.sha256(path.read_bytes()).digest() == expected for path in wrappers)
    env = Path(f'/proc/{observer_pid}/environ').read_bytes().split(b'\0')
    assert not any(value.startswith(b'LD_PRELOAD=') and value != b'LD_PRELOAD=' for value in env)
    assert os.stat(f'/proc/{observer_pid}/ns/ipc').st_ino != os.stat('/proc/self/ns/ipc').st_ino

    # Registration and a metadata writer are not evidence of work completion.
    assert not (directory / 'received.json').exists()
    for count in range(1, 4):
        command(directory, 'writer', 'publish', count=1)
        wait_for(lambda: read_json(directory / 'received.json'), lambda values: values == list(range(1, count + 1)))
    initial = wait_for(lambda: command(directory, 'writer', 'snapshot'),
        lambda state: topic(state)['subscribers'][0]['received_total'] == 3 and
                      topic(state)['subscribers'][0]['retained_entries'] == 0)
    assert topic(initial)['publishers'][0]['published_total'] == 3
    assert topic(initial)['subscribers'][0]['dropped_history_total'] == 0
    assert topic(initial)['type_name'] == 'sensor_msgs/msg/Image'
    assert topic(initial)['publishers'][0]['pid'] == writer['pid']
    assert topic(initial)['subscribers'][0]['pid'] == reader['pid']

    # Freeze only this directly owned test peer, then resume it before owner cleanup.
    os.kill(reader['pid'], signal.SIGSTOP)
    try:
        wait_for(lambda: Path(f'/proc/{reader["pid"]}/stat').read_text().rsplit(')', 1)[1].split()[0],
                 lambda value: value == 'T')
        command(directory, 'writer', 'publish', count=5)
        before = command(directory, 'writer', 'snapshot')
        entry = topic(before)['subscribers'][0]
        assert (entry['received_total'], entry['dropped_history_total'], entry['queued_entries']) == (3, 3, 2)
        assert topic(before)['publishers'][0]['published_total'] == 8
        remote = wait_for(lambda: state_for(query(settings), writer['session']),
            lambda state: state is not None and topic(state)['publishers'][0]['published_total'] == 8)
        assert remote['ipc_ns_inode'] == writer['ipc']
        assert counters(remote) == counters(before)
        cli = subprocess.run(['ros2', 'topic', 'stats_hermos', TOPIC,
            '--session-id', writer['session'], '--gossip-timeout', '4'],
            env=settings, capture_output=True, text=True, timeout=10)
        assert cli.returncode == 0, cli.stdout + cli.stderr
        assert 'published_total' in cli.stdout and 'dropped_history_total' in cli.stdout
        after = command(directory, 'writer', 'snapshot')
        assert counters(after) == counters(before)
    finally:
        os.kill(reader['pid'], signal.SIGCONT)
    wait_for(lambda: read_json(directory / 'received.json'), lambda values: values == [1, 2, 3, 7, 8])
    command(directory, 'reader', 'close')
    closed = command(directory, 'writer', 'snapshot')
    assert not topic(closed)['subscribers']
    assert len(topic(closed)['publishers']) == 1
    result = epoch.stop()
    assert result['verified_teardown'] and not result['peer_failed']


def test_same_topic_sessions_remain_separate_and_restart_gets_new_identity(epochs):
    first, first_dir, settings = epochs(('writer',))
    second, second_dir, _ = epochs(('writer',))
    first_id = read_json(first_dir / 'writer-ready.json')['session']
    second_id = read_json(second_dir / 'writer-ready.json')['session']
    assert first_id != second_id
    states = wait_for(lambda: query(settings),
        lambda values: {first_id, second_id} <= {state['session_id'] for state in values})
    selected = [state for state in states if state['session_id'] in (first_id, second_id)]
    assert len({state['ipc_ns_inode'] for state in selected}) == 2
    assert all(len(topic(state)['publishers']) == 1 and not topic(state)['subscribers'] for state in selected)
    first.stop()
    third, third_dir, _ = epochs(('writer',))
    third_id = read_json(third_dir / 'writer-ready.json')['session']
    assert third_id not in (first_id, second_id)
    latest = wait_for(lambda: query(settings),
        lambda values: {second_id, third_id} <= {state['session_id'] for state in values})
    assert first_id not in {state['session_id'] for state in latest}


def test_same_topic_in_two_domains_is_not_cross_reported(epochs):
    first_domain = 180 + os.getpid() % 30
    _, first_dir, first_env = epochs(('writer',), domain=first_domain)
    _, second_dir, second_env = epochs(('writer',), domain=first_domain + 1)
    first_id = read_json(first_dir / 'writer-ready.json')['session']
    second_id = read_json(second_dir / 'writer-ready.json')['session']
    for environment, expected, other, domain in (
            (first_env, first_id, second_id, first_domain),
            (second_env, second_id, first_id, first_domain + 1)):
        states = wait_for(lambda: query(environment),
            lambda values: expected in {state['session_id'] for state in values})
        assert other not in {state['session_id'] for state in states}
        observed = state_for(states, expected)
        assert observed['domain_id'] == topic(observed)['domain_id'] == domain
        assert len(topic(observed)['publishers']) == 1
        assert not topic(observed)['subscribers']


def test_peer_crash_tears_down_observer_and_new_epoch_has_fresh_session(epochs):
    first, directory, environment = epochs(('writer',))
    writer = read_json(directory / 'writer-ready.json')
    wait_for(lambda: state_for(query(environment), writer['session']))
    os.kill(writer['pid'], signal.SIGKILL)
    result = first.wait(timeout=30)
    assert result['verified_teardown'] and result['peer_failed']
    assert writer['session'] not in {state['session_id'] for state in query(environment)}
    _, next_directory, _ = epochs(('writer',))
    replacement = read_json(next_directory / 'writer-ready.json')['session']
    assert replacement != writer['session']
    state = wait_for(lambda: state_for(query(environment), replacement))
    assert len(topic(state)['publishers']) == 1 and not topic(state)['subscribers']


def test_removed_publisher_is_hidden_while_reader_alias_preserves_payload(epochs):
    _, directory, _ = epochs()
    command(directory, 'reader', 'retain')
    command(directory, 'writer', 'publish', count=1)
    wait_for(lambda: read_json(directory / 'received.json'), lambda values: values == [1])
    held = command(directory, 'reader', 'snapshot')
    assert topic(held)['subscribers'][0]['retained_entries'] == 1
    try:
        assert command(directory, 'writer', 'close')['closed']
        removed = command(directory, 'reader', 'snapshot')
        assert not topic(removed)['publishers']
        assert topic(removed)['subscribers'][0]['retained_entries'] == 1
    finally:
        released = command(directory, 'reader', 'release')
    assert released['contents'] == [[1] * 16]
    wait_for(lambda: command(directory, 'reader', 'snapshot'),
        lambda state: topic(state)['subscribers'][0]['retained_entries'] == 0)


def test_metadata_agent_duplicate_is_rejected_before_group_startup(epochs, tmp_path):
    with pytest.raises(ValueError, match='metadata peer.*ROS domain'):
        epochs((), duplicate=True)
    assert not (tmp_path / 'case-0/epoch').exists()
