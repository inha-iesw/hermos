"""Source-free Python/C++ payload oracle, copied beside installed test apps."""

import json
from pathlib import Path
import sys
import time
import uuid

from ament_index_python.packages import get_package_prefix
from hermos_runtime.supervision.isolated import IsolatedEpoch
from support import Protocol, successful


def cpu_factory(context, arguments):
    """Exercise native CPU endpoint setup and launcher-owned signal teardown."""
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from rclpy_hermos import Node
    from sensor_msgs.msg import Image

    node = Node('installed_cpu_peer', context=context)
    node.create_publisher(Image, '/installed_cpu',
                          QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT),
                          transport='hermos', max_message_bytes=4096)
    ready = False

    def announce():
        nonlocal ready
        if not ready:
            print('READY cpu', flush=True)
            ready = True

    node.create_timer(0.005, announce)
    return node


def main():
    producer, consumer, stage = sys.argv[1:]
    topic = '/installed_gpu_' + uuid.uuid4().hex
    library = Path(get_package_prefix('hermoslib')) / 'lib'
    environment = dict(HERMOS_DEVICE_PATH='/dev/hermos_rclpy', HERMOS_BRIDGE_MODE='off',
                       HERMOS_NO_DISCOVERY_AGENT='1',
                       LD_PRELOAD=str(library / 'libhermos_heaphook.so'))
    commands = []
    for language, role in ((producer, 'publisher'), (consumer, 'subscriber')):
        if language == 'cpp':
            argv = [str(Path(__file__).with_name('cpp_peer')), role, topic, '1']
        else:
            argv = [sys.executable, '-u', '-m', 'rclpy_hermos_gpu.launch',
                    '--blocks', '1' if role == 'publisher' else '0',
                    '--block-bytes', '64', '--max-reads', '2', '--max-message-bytes', '64',
                    'tensor_peer:factory', role, topic, 'uint8']
        commands.append(dict(argv=argv, env=environment))
    cpu_entry = Path(get_package_prefix('hermos_runtime')) / 'lib/hermos_runtime/hermos_cpu_peer'
    commands.append(dict(argv=[str(cpu_entry), 'peer_group:cpu_factory'], env=environment))
    epoch = IsolatedEpoch(commands, Path(stage), startup_timeout=30, stop_timeout=5)
    loaded = {}
    try:
        epoch.start()
        protocol = Protocol(epoch)
        for index in range(2):
            assert protocol.receive(index).startswith('READY')
        deadline = time.monotonic() + 5
        while 'READY cpu' not in (epoch.epoch_dir / 'peer-2.stdout').read_text():
            assert time.monotonic() < deadline
            time.sleep(.005)
        cpu_manifest = json.loads((epoch.epoch_dir / 'peer-2.json').read_text())
        assert cpu_manifest['resources'] == [] and cpu_manifest['pid'] == epoch.peers[2]
        for pid in [epoch.worker_pid, *epoch.peers]:
            maps = Path(f'/proc/{pid}/maps').read_text()
            assert '/workspace/' not in maps, maps
            loaded[str(pid)] = sorted({line.split()[-1] for line in maps.splitlines()
                                      if '/' in line and 'hermos' in line})
        for value in (3, 17, 91):
            protocol.publish(value)
            if consumer == 'cpp':
                assert protocol.ask(1, 'take').startswith('MAPPED')
                assert protocol.ask(1, 'finish') == f'DONE {value * 64}'
            else:
                answer = protocol.receive(1)
                assert answer.startswith('RECEIVED ')
                result = json.loads(answer.split(' ', 1)[1])
                assert result['sum'] == value * 64 and result['dtype'] == 'torch.uint8'
        for index, language in enumerate((producer, consumer)):
            if language == 'cpp':
                epoch.send(index, 'quit')
        result = epoch.wait(timeout=10) if 'cpp' in (producer, consumer) else epoch.stop()
        successful(result)
    finally:
        epoch.stop()
    print(json.dumps(dict(loaded=loaded, result=result)))


if __name__ == '__main__':
    main()
