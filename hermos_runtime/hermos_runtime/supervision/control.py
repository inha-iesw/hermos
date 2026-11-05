"""Bounded pipe writes and one buffered JSON-line reader for worker control."""

import json
import os
import select
import time


def write_pipe(fd, data, timeout):
    """Write all bytes by a deadline without blocking the lifecycle monitor."""
    os.set_blocking(fd, False)
    remaining = memoryview(data)
    deadline = time.monotonic() + timeout
    while remaining:
        delay = deadline - time.monotonic()
        if delay <= 0 or not select.select([], [fd], [], delay)[1]:
            raise TimeoutError('pipe write deadline expired')
        try:
            remaining = remaining[os.write(fd, remaining):]
        except BlockingIOError:
            continue


class JsonInput:
    """Keep read-ahead bytes here; never mix this with TextIOWrapper.readline."""

    def __init__(self, fd):
        self.fd, self.pending = fd, b''

    def read(self, timeout):
        deadline = time.monotonic() + timeout
        while b'\n' not in self.pending:
            delay = max(0, deadline - time.monotonic())
            if not select.select([self.fd], [], [], delay)[0]:
                raise TimeoutError('control read deadline expired')
            data = os.read(self.fd, 65536)
            if not data:
                raise EOFError('owner pipe closed')
            self.pending += data
        line, self.pending = self.pending.split(b'\n', 1)
        return json.loads(line)
