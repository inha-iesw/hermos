"""Host-only launcher failure containment, using real signals in owned processes."""

from contextlib import contextmanager
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

from rclpy_hermos_gpu import launch
from rclpy_hermos_gpu import runtime as runtime_module


@pytest.fixture(scope='module')
def blocking_library(tmp_path_factory):
    """Stay inside a GIL-releasing C call even when its sleep is interrupted."""
    directory = tmp_path_factory.mktemp('gpu-launch-blocking')
    source = directory / 'blocking.c'
    library = directory / 'blocking.so'
    source.write_text('''
#include <errno.h>
#include <signal.h>
#include <time.h>
#include <unistd.h>

static struct sigaction previous_term;
static volatile sig_atomic_t native_stopped;

static void native_term(int number, siginfo_t *info, void *context)
{
  native_stopped = 1;
  if (previous_term.sa_flags & SA_SIGINFO) {
    previous_term.sa_sigaction(number, info, context);
  } else if (previous_term.sa_handler != SIG_DFL && previous_term.sa_handler != SIG_IGN) {
    previous_term.sa_handler(number);
  }
}

int install_native_term(void)
{
  struct sigaction action = {0};
  action.sa_sigaction = native_term;
  action.sa_flags = SA_SIGINFO;
  sigemptyset(&action.sa_mask);
  native_stopped = 0;
  return sigaction(SIGTERM, &action, &previous_term);
}

int native_executor_stopped(void) {return native_stopped;}

void block_python(void)
{
  const char marker[] = "BLOCKED\\n";
  (void)write(STDOUT_FILENO, marker, sizeof(marker) - 1);
  for (;;) {
    struct timespec remaining;
    struct timespec request = {60, 0};
    while (nanosleep(&request, &remaining) != 0 && errno == EINTR) {
      request = remaining;
    }
  }
}
''', encoding='utf-8')
    compiler = shutil.which('cc') or shutil.which('gcc')
    if compiler is None:
        pytest.skip('a host C compiler is required for the blocked native-call oracle')
    subprocess.run([compiler, '-shared', '-fPIC', str(source), '-o', str(library)],
                   check=True, capture_output=True, text=True, timeout=30)
    return library


@contextmanager
def child_process(code, *arguments):
    environment = os.environ.copy()
    package_root = str(Path(launch.__file__).resolve().parents[1])
    environment['PYTHONPATH'] = os.pathsep.join(
        [package_root, environment.get('PYTHONPATH', '')])
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    process = subprocess.Popen(
        [sys.executable, '-u', '-c', code, *map(str, arguments)], env=environment,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def marker(process, expected, timeout=10):
    """Avoid a hanging test if the child fails before announcing its blocking state."""
    readable, _, _ = select.select([process.stdout], [], [], timeout)
    assert readable, f'child did not announce {expected!r}; status={process.poll()}'
    actual = process.stdout.readline().strip()
    assert actual == expected, f'expected {expected!r}, received {actual!r}'


@pytest.mark.parametrize('blocked_native', [False, True], ids=['noop-handler', 'native-call'])
def test_term_deadline_exits_70_while_main_does_not_unwind(blocking_library, blocked_native):
    # Native-call case announces entry from C. A Python-level handler can therefore
    # never execute before the watchdog terminates that process.
    code = '''
import ctypes
import signal
import sys
import time
from rclpy_hermos_gpu.launch import _TerminationDeadline

def handled(number, frame):
    print('HANDLER', flush=True)

signal.signal(signal.SIGTERM, handled)
watchdog = _TerminationDeadline()
if sys.argv[2] == 'native':
    library = ctypes.CDLL(sys.argv[1])
    library.block_python.argtypes = []
    library.block_python.restype = None
    library.block_python()
else:
    print('BLOCKED', flush=True)
    time.sleep(60)
raise AssertionError('blocking call returned before the watchdog')
'''
    with child_process(code, blocking_library, 'native' if blocked_native else 'python') as process:
        marker(process, 'BLOCKED')
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=7)
        elapsed = time.monotonic() - started
        assert process.returncode == 70, (stdout, stderr)
        assert 2.7 <= elapsed < 5.0, elapsed
        assert 'TERM deadline expired' in stderr
        assert 'teardown incomplete' in stderr
        if blocked_native:
            assert 'HANDLER' not in stdout
        else:
            assert 'HANDLER' in stdout


def test_normal_close_after_term_cancels_deadline_and_restores_previous_wakeup_fd():
    code = '''
import os
import signal
import sys
import time
from rclpy_hermos_gpu.launch import _TerminationDeadline

previous_read, previous_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
signal.set_wakeup_fd(previous_write)
signal.signal(signal.SIGTERM, lambda number, frame: print('HANDLER', flush=True))
watchdog = _TerminationDeadline()
print('READY', flush=True)
assert sys.stdin.readline().strip() == 'close'
watchdog.close()
assert not watchdog.thread.is_alive()
assert signal.set_wakeup_fd(-1) == previous_write
os.close(previous_read)
os.close(previous_write)
print('CLOSED', flush=True)
time.sleep(3.3)
print('SURVIVED', flush=True)
'''
    with child_process(code) as process:
        marker(process, 'READY')
        process.send_signal(signal.SIGTERM)
        marker(process, 'HANDLER')
        process.stdin.write('close\n')
        process.stdin.flush()
        marker(process, 'CLOSED')
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, (stdout, stderr)
        assert 'SURVIVED' in stdout
        assert 'deadline expired' not in stderr


@pytest.mark.parametrize('mode', ['normal-context-stop', 'async-poison', 'native-signal-installed'])
def test_launcher_polls_without_callbacks_and_cleans_up_before_return(
        monkeypatch, mode, capsys, blocking_library):
    import ctypes

    poison = mode == 'async-poison'
    library = ctypes.CDLL(str(blocking_library))
    events = []
    before_handlers = {number: signal.getsignal(number)
                       for number in (signal.SIGINT, signal.SIGTERM)}

    class Context:
        def __init__(self):
            self.active = True

        def ok(self):
            return self.active

        def shutdown(self):
            events.append('context-close')
            self.active = False

    class Node:
        def destroy_node(self):
            events.append('node-close')

    class Executor:
        def __init__(self, *, context):
            self.context = context
            self.nodes = []

        def add_node(self, node):
            self.nodes.append(node)
            events.append('add-node')

        def get_nodes(self):
            return list(self.nodes)

        def spin_once(self, *, timeout_sec):
            assert timeout_sec == 0.01
            events.append('spin-without-callback')
            if mode == 'native-signal-installed':
                os.kill(os.getpid(), signal.SIGTERM)
                if library.native_executor_stopped():
                    raise RuntimeError('subscriber executor is stopped')
                assert self.context.ok()
            else:
                self.context.active = False

        def shutdown(self, *, timeout_sec):
            assert timeout_sec == 0
            events.append('executor-close')

    class Runtime:
        def __init__(self, **configuration):
            assert configuration == dict(block_count=0, block_bytes=4096, device=0,
                                         max_reads=2, max_message_bytes=4096)

        def initialise(self):
            events.append('initialise')

        def register(self):
            events.append('register')

        def poll(self):
            events.append('poll')
            if poison:
                raise RuntimeError('asynchronous terminal poison')

        def shutdown(self, *, timeout, progress):
            assert timeout == 3.0 and callable(progress)
            events.append('runtime-close')

    rclpy = ModuleType('rclpy')

    def init(*, args, context, signal_handler_options):
        assert args == [] and context.ok()
        assert signal_handler_options == 'no-signal-handlers'
        events.append('rclpy-init')

    def factory(runtime, arguments):
        assert runtime.ros_context.ok()
        assert arguments == ['application-argument']
        events.append('factory')
        if mode == 'native-signal-installed':
            # Hermos init installs a native handler that chains CPython's signal
            # handler and stops its worker. signal.getsignal() cannot see this swap.
            assert library.install_native_term() == 0
        return Node()

    rclpy.init = init
    for name, module in {
        'rclpy': rclpy,
        'rclpy.context': SimpleNamespace(Context=Context),
        'rclpy.executors': SimpleNamespace(
            SingleThreadedExecutor=Executor, TimeoutException=TimeoutError,
            ShutdownException=InterruptedError, ExternalShutdownException=ConnectionError),
        'rclpy.client': SimpleNamespace(Client=type('Client', (), {})),
        'rclpy.node': SimpleNamespace(Node=Node),
        'rclpy.signals': SimpleNamespace(
            SignalHandlerOptions=SimpleNamespace(NO='no-signal-handlers')),
        'g4_test_application': SimpleNamespace(factory=factory),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(runtime_module, 'GpuRuntime', Runtime)
    status = launch.main([
        '--block-bytes', '4096', '--max-reads', '2', '--max-message-bytes', '4096',
        'g4_test_application:factory', 'application-argument'])
    assert status == (70 if poison else 0)
    assert events[:6] == ['rclpy-init', 'initialise', 'factory', 'add-node', 'register', 'poll']
    assert events.index('runtime-close') < events.index('executor-close') < events.index('node-close')
    if poison:
        assert 'spin-without-callback' not in events
        assert events[-1] == 'context-close'
        assert 'asynchronous terminal poison' in capsys.readouterr().err
    else:
        assert 'spin-without-callback' in events
    assert {number: signal.getsignal(number) for number in before_handlers} == before_handlers
