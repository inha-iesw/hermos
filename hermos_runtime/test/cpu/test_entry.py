"""Exercise real exec, PID/argv preservation and signal delivery without a device."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from hermos_runtime.cpu import entry
from hermos_runtime.environment import Installation, InstallationError


@pytest.fixture
def executable(tmp_path):
    source = tmp_path / 'allocator.c'
    source.write_text('''
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <fcntl.h>
#include <unistd.h>
extern void *__libc_malloc(size_t);
extern void *__libc_calloc(size_t, size_t);
extern void *__libc_realloc(void *, size_t);
extern void __libc_free(void *);
void *malloc(size_t n) { return __libc_malloc(n); }
void *calloc(size_t n, size_t s) { return __libc_calloc(n, s); }
void *realloc(void *p, size_t n) { return __libc_realloc(p, n); }
void free(void *p) { __libc_free(p); }
__attribute__((constructor)) static void register_pid(void) {
    const char *directory = getenv("HERMOS_TEST_REGISTRY");
    if (directory) {
        char path[4096];
        snprintf(path, sizeof(path), "%s/%d", directory, getpid());
        int fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
        if (fd < 0) _exit(99);
        close(fd);
    }
}
''')
    hook = tmp_path / 'libhermos_heaphook.so'
    subprocess.run(['cc', '-shared', '-fPIC', str(source), '-o', str(hook)], check=True)
    app = tmp_path / 'cpu_fixture.py'
    app.write_text('''
import json, os, signal, sys
from hermos_runtime.environment.process import malloc_provider
def main():
    print(json.dumps(dict(pid=os.getpid(), argv=sys.argv[1:], malloc=str(malloc_provider()))), flush=True)
    if '--wait' in sys.argv:
        signal.pause()
    return 23
''')
    wrapper = tmp_path / 'wrapper'
    wrapper.write_text('''
from pathlib import Path
import os
from hermos_runtime.environment import Installation
from hermos_runtime.cpu import entry
base = Path(__file__).parent
installation = Installation(base / 'libhermos_heaphook.so', base / 'libhermos.so',
    Path('/dev/hermos_rclpy'), dict(registry=base / 'librclpy_hermos_types.so',
    native=base / '_native.so'), dict(module='hermos_rclpy'))
entry.resolve_installation = lambda **kwargs: installation
entry.check_device = lambda _: None
entry.check_python_origin = lambda _: None
print('BOOT ' + str(os.getpid()), flush=True)
if os.environ.get('TEST_NATIVE') == '1':
    raise SystemExit(entry.run_executable(base / 'native-peer'))
raise SystemExit(entry.run('cpu_fixture:main'))
''')
    environment = dict(os.environ)
    environment.pop('LD_PRELOAD', None)
    environment.pop('_HERMOS_CPU_EXEC', None)
    environment['PYTHONPATH'] = str(tmp_path) + os.pathsep + environment.get('PYTHONPATH', '')
    registry = tmp_path / 'registrations'
    registry.mkdir()
    environment['HERMOS_TEST_REGISTRY'] = str(registry)
    return wrapper, hook, environment


def test_real_exec_preserves_pid_argv_and_application_exit(executable):
    wrapper, hook, environment = executable
    result = subprocess.run([sys.executable, str(wrapper), 'space value', '한글', '--ros-args',
                             '-r', '__ns:=/test'], env=environment, text=True, capture_output=True)
    assert result.returncode == 23, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 3
    assert lines[0] == lines[1]
    report = json.loads(lines[2])
    assert report['pid'] == int(lines[0].split()[1])
    assert report['argv'] == ['space value', '한글', '--ros-args', '-r', '__ns:=/test']
    assert report['malloc'] == str(hook)


def test_correct_preload_does_not_exec_again(executable):
    wrapper, hook, environment = executable
    environment.update(LD_PRELOAD=str(hook), LD_LIBRARY_PATH=str(hook.parent),
        HERMOS_DEVICE_PATH='/dev/hermos_rclpy', HERMOS_BRIDGE_MODE='off',
        HERMOS_NO_DISCOVERY_AGENT='1')
    result = subprocess.run([sys.executable, str(wrapper)], env=environment,
                            text=True, capture_output=True)
    assert result.returncode == 23, result.stderr
    assert len(result.stdout.splitlines()) == 2


def test_preloaded_process_with_stale_loader_path_fails_without_second_registration(executable):
    wrapper, hook, environment = executable
    environment.update(LD_PRELOAD=str(hook), LD_LIBRARY_PATH='/stale/prefix/lib')
    result = subprocess.run([sys.executable, str(wrapper)], env=environment,
                            text=True, capture_output=True)
    assert result.returncode == 78, result.stderr
    assert 'already initialized' in result.stderr
    assert len(result.stdout.splitlines()) == 1


def test_failed_exec_marker_cannot_cause_a_retry_loop(executable):
    wrapper, _, environment = executable
    environment['_HERMOS_CPU_EXEC'] = '1'
    result = subprocess.run([sys.executable, str(wrapper)], env=environment,
                            text=True, capture_output=True)
    assert result.returncode == 78
    assert 'refusing to retry' in result.stderr
    assert len(result.stdout.splitlines()) == 1


def test_sigterm_reaches_the_execed_application(executable):
    wrapper, _, environment = executable
    process = subprocess.Popen([sys.executable, str(wrapper), '--wait'], env=environment,
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert process.stdout.readline().startswith('BOOT ')
        assert process.stdout.readline().startswith('BOOT ')
        report = json.loads(process.stdout.readline())
        assert report['pid'] == process.pid
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5) == -signal.SIGTERM
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_mixed_loaded_registry_fails_before_application(monkeypatch, tmp_path):
    hook = tmp_path / 'libhermos_heaphook.so'
    hook.touch()
    installed = Installation(hook, tmp_path / 'libhermos.so', Path('/dev/hermos_rclpy'),
        dict(registry=tmp_path / 'librclpy_hermos_types.so', native=tmp_path / '_native.so'), {})
    monkeypatch.setattr(entry, 'resolve_installation', lambda **_: installed)
    monkeypatch.setattr(entry, 'check_device', lambda _: None)
    monkeypatch.setattr(entry, 'check_extra_preloads', lambda *_: None)
    monkeypatch.setattr(entry, 'loaded_paths', lambda: {Path('/stale/librclpy_hermos_types.so')})
    monkeypatch.delenv('LD_PRELOAD', raising=False)
    with pytest.raises(InstallationError, match='registry'):
        entry._prepare(())


def test_native_exec_registers_pid_only_in_final_program(executable):
    wrapper, hook, environment = executable
    source = wrapper.parent / 'native.c'
    source.write_text('#include <stdio.h>\n#include <unistd.h>\n'
                      'int main(int argc, char **argv) {'
                      'printf("NATIVE %d %s\\n", getpid(), argv[1]); return 27; }\n')
    subprocess.run(['cc', str(source), '-o', str(wrapper.parent / 'native-peer')], check=True)
    environment['TEST_NATIVE'] = '1'
    result = subprocess.run([sys.executable, str(wrapper), 'space value'], env=environment,
                            text=True, capture_output=True)
    assert result.returncode == 27, result.stderr
    boot, native = result.stdout.splitlines()
    pid = int(boot.split()[1])
    assert native == f'NATIVE {pid} space value'
    assert (Path(environment['HERMOS_TEST_REGISTRY']) / str(pid)).is_file()


def test_native_wrapper_rejects_preloaded_interpreter(executable):
    wrapper, hook, environment = executable
    target = wrapper.parent / 'native-peer'
    target.symlink_to('/usr/bin/true')
    environment.update(TEST_NATIVE='1', LD_PRELOAD=str(hook), LD_LIBRARY_PATH=str(hook.parent),
        HERMOS_DEVICE_PATH='/dev/hermos_rclpy', HERMOS_BRIDGE_MODE='off',
        HERMOS_NO_DISCOVERY_AGENT='1')
    result = subprocess.run([sys.executable, str(wrapper)], env=environment,
                            text=True, capture_output=True)
    assert result.returncode == 78, result.stderr
    assert 'same PID twice' in result.stderr
    assert len(result.stdout.splitlines()) == 1
