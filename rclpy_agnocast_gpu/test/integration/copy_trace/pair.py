"""Two production-launcher peers profiled as one diagnostic process tree."""

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from support import Protocol, peer


class Pair:
    def __init__(self, stage):
        self.epoch_dir = stage
        self.processes = []
        self.files = []

    def start(self):
        topic = '/g4_copy_' + uuid.uuid4().hex
        for index, role in enumerate(('publisher', 'subscriber')):
            spec = peer('python', role, topic)
            environment = {**os.environ, **spec['env'], 'G4_TRACE_REGION': '1'}
            for name in ('G3_RECOVERY_MANIFEST', 'G3_RECOVERY_ACK'):
                environment.pop(name, None)
            output = (self.epoch_dir / f'peer-{index}.stdout').open('w')
            error = (self.epoch_dir / f'peer-{index}.stderr').open('w')
            self.files.extend((output, error))
            self.processes.append(subprocess.Popen(
                spec['argv'], env=environment, stdin=subprocess.PIPE,
                stdout=output, stderr=error, text=True))

    def poll(self):
        for process in self.processes:
            code = process.poll()
            if code is not None:
                return code
        return None

    def send(self, index, command):
        stream = self.processes[index].stdin
        stream.write(command + '\n')
        stream.flush()

    def close(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdin.close()
        for stream in self.files:
            stream.close()
        return [process.returncode for process in self.processes]


stage = Path(sys.argv[1])
pair = Pair(stage)
result = dict(diagnostic_only=True, dtype='uint8', payload_bytes=64, received=[])
try:
    pair.start()
    protocol = Protocol(pair)
    assert protocol.receive(0).startswith('READY')
    assert protocol.receive(1).startswith('READY')
    result['pids'] = [process.pid for process in pair.processes]
    for value in (3, 7, 11, 17, 29, 41, 53, 91):
        protocol.publish(value)
        response = protocol.receive(1)
        assert response.startswith('RECEIVED ')
        data = json.loads(response.split(' ', 1)[1])
        assert data['sum'] == 64 * value and data['dtype'] == 'torch.uint8'
        result['received'].append(data)
    result['data_pass'] = True
finally:
    result['returncodes'] = pair.close()
    (stage / 'pair.json').write_text(json.dumps(result, indent=2) + '\n')
assert result['returncodes'] == [0, 0], result
