"""Observe driver-owned CUDA shared memory after explicit event cleanup.

This diagnostic never unlinks shared-memory objects or resets the GPU. Run it
without concurrent GPU tests so the before/after inventory can be attributed.
"""

import argparse
import ctypes
import json
import os
import select
from pathlib import Path
import subprocess
import sys


class IpcHandle(ctypes.Structure):
    _fields_ = [('data', ctypes.c_ubyte * 64)]


def child(mode, encoded=None):
    """Create/import one event with ordered exporter/importer cleanup."""
    cuda = ctypes.CDLL('libcuda.so.1')
    pointer = ctypes.c_void_p
    signatures = {
        'cuInit': [ctypes.c_uint],
        'cuDevicePrimaryCtxRetain': [ctypes.POINTER(pointer), ctypes.c_int],
        'cuCtxPushCurrent_v2': [pointer],
        'cuEventCreate': [ctypes.POINTER(pointer), ctypes.c_uint],
        'cuIpcGetEventHandle': [pointer, pointer],
        'cuIpcOpenEventHandle': [ctypes.POINTER(pointer), IpcHandle],
        'cuEventDestroy_v2': [pointer],
        'cuEventRecord': [pointer, pointer], 'cuEventSynchronize': [pointer],
        'cuCtxPopCurrent_v2': [ctypes.POINTER(pointer)],
        'cuDevicePrimaryCtxRelease_v2': [ctypes.c_int],
    }
    calls = []

    def invoke(name, *args):
        function = getattr(cuda, name)
        function.argtypes, function.restype = signatures[name], ctypes.c_int
        result = function(*args)
        calls.append([name, result])
        if result:
            raise RuntimeError(f'{name}: {result}')

    context, event = pointer(), pointer()
    invoke('cuInit', 0)
    invoke('cuDevicePrimaryCtxRetain', ctypes.byref(context), 0)
    try:
        invoke('cuCtxPushCurrent_v2', context)
        try:
            # CU_EVENT_DISABLE_TIMING | CU_EVENT_INTERPROCESS.
            if mode in ('import', 'crash_import'):
                handle = IpcHandle.from_buffer_copy(bytes.fromhex(encoded))
                invoke('cuIpcOpenEventHandle', ctypes.byref(event), handle)
            else:
                invoke('cuEventCreate', ctypes.byref(event), 6)
            try:
                if mode not in ('import', 'crash_import'):
                    invoke('cuEventRecord', event, None)
                    invoke('cuEventSynchronize', event)
                if mode in ('export', 'producer'):
                    handle = IpcHandle()
                    invoke('cuIpcGetEventHandle', ctypes.byref(handle), event)
                    if mode == 'producer':
                        print(json.dumps({'pid': os.getpid(), 'handle': bytes(handle).hex()}),
                              flush=True)
                        if sys.stdin.readline().strip() != 'close':
                            raise RuntimeError('missing importer completion')
                if mode == 'crash_import':
                    print(json.dumps({'pid': os.getpid(), 'calls': calls, 'mode': mode}), flush=True)
                    os._exit(0)
            finally:
                invoke('cuEventDestroy_v2', event)
        finally:
            restored = pointer()
            invoke('cuCtxPopCurrent_v2', ctypes.byref(restored))
            if restored.value != context.value:
                raise RuntimeError('unexpected popped context')
    finally:
        invoke('cuDevicePrimaryCtxRelease_v2', 0)
    print(json.dumps({'pid': os.getpid(), 'calls': calls, 'mode': mode}))


def runtime_child(mode):
    """Match the probe's runtime stream and pinned-host/device allocations."""
    runtime = ctypes.CDLL('/usr/local/cuda/lib64/libcudart.so')
    pointer = ctypes.c_void_p
    signatures = {'cudaSetDevice': [ctypes.c_int],
                  'cudaStreamCreate': [ctypes.POINTER(pointer)],
                  'cudaStreamDestroy': [pointer],
                  'cudaHostAlloc': [ctypes.POINTER(pointer), ctypes.c_size_t, ctypes.c_uint],
                  'cudaFreeHost': [pointer], 'cudaMalloc': [ctypes.POINTER(pointer), ctypes.c_size_t],
                  'cudaFree': [pointer], 'cudaDeviceSynchronize': [],
                  'cudaEventCreateWithFlags': [ctypes.POINTER(pointer), ctypes.c_uint],
                  'cudaEventRecord': [pointer, pointer], 'cudaEventSynchronize': [pointer],
                  'cudaIpcGetEventHandle': [pointer, pointer], 'cudaEventDestroy': [pointer],
                  'cudaLaunchHostFunc': [pointer, ctypes.CFUNCTYPE(None, pointer), pointer]}
    calls = []

    def invoke(name, *args):
        function = getattr(runtime, name)
        function.argtypes, function.restype = signatures[name], ctypes.c_int
        result = function(*args)
        calls.append([name, result])
        if result:
            raise RuntimeError(f'{name}: {result}')

    invoke('cudaSetDevice', 0)
    stream, host, device = pointer(), pointer(), pointer()
    if mode == 'runtime_event':
        event, handle = pointer(), IpcHandle()
        invoke('cudaEventCreateWithFlags', ctypes.byref(event), 6)
        try:
            invoke('cudaEventRecord', event, None)
            invoke('cudaEventSynchronize', event)
            invoke('cudaIpcGetEventHandle', ctypes.byref(handle), event)
        finally:
            invoke('cudaEventDestroy', event)
    elif mode != 'runtime_init':
        invoke('cudaStreamCreate', ctypes.byref(stream))
        try:
            invoke('cudaHostAlloc', ctypes.byref(host), 4, 0)
            try:
                invoke('cudaMalloc', ctypes.byref(device), 4)
                try:
                    if mode == 'runtime_callback':
                        callback = ctypes.CFUNCTYPE(None, pointer)(lambda argument: None)
                        invoke('cudaLaunchHostFunc', stream, callback, None)
                    invoke('cudaDeviceSynchronize')
                finally:
                    invoke('cudaFree', device)
            finally:
                invoke('cudaFreeHost', host)
        finally:
            invoke('cudaStreamDestroy', stream)
    print(json.dumps({'pid': os.getpid(), 'calls': calls, 'mode': mode}), flush=True)
    if mode == 'runtime_exit':
        os._exit(0)


def pair(crash):
    """Keep the exporting event alive until its importer has exited."""
    source = str(Path(__file__).resolve())
    producer = subprocess.Popen([sys.executable, source, '--child', 'producer'],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    try:
        if not select.select([producer.stdout], [], [], 20)[0]:
            raise TimeoutError('producer startup deadline')
        ready = json.loads(producer.stdout.readline())
        imported = subprocess.run(
            [sys.executable, source, '--child', 'crash_import' if crash else 'import',
             '--handle', ready['handle']], capture_output=True, text=True, timeout=20)
        stdout, stderr = producer.communicate('close\n', timeout=20)
        return {'producer': {'returncode': producer.returncode, 'stdout': stdout, 'stderr': stderr},
                'importer': {'returncode': imported.returncode,
                             'stdout': imported.stdout, 'stderr': imported.stderr}}
    finally:
        if producer.poll() is None:
            producer.kill()
            producer.communicate(timeout=20)


def inventory():
    """Read CUDA driver names and sizes without assuming ownership."""
    return {str(path): {'bytes': path.stat().st_size, 'inode': path.stat().st_ino}
            for path in Path('/dev/shm').glob('cuda.shm.*')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path)
    parser.add_argument('--child', choices=('event', 'export', 'producer', 'import', 'crash_import',
                                                   'runtime_init', 'runtime', 'runtime_exit', 'runtime_callback',
                                                   'runtime_event', 'mixed_event'))
    parser.add_argument('--handle')
    args = parser.parse_args()
    if args.child:
        if args.child == 'mixed_event':
            runtime_child('runtime_init')
            child('export')
        elif args.child.startswith('runtime'):
            runtime_child(args.child)
        else:
            child(args.child, args.handle)
        return
    if args.stage is None or not args.stage.is_absolute():
        parser.error('--stage must be an absolute new directory')
    args.stage.mkdir(exist_ok=False)
    record = {'before': inventory(), 'cycles': []}
    for mode in ('event', 'export', 'pair', 'crash_pair',
                 'runtime_init', 'runtime', 'runtime_callback', 'runtime_exit',
                 'runtime_event', 'runtime_event', 'mixed_event', 'mixed_event'):
        before = inventory()
        if mode in ('pair', 'crash_pair'):
            details = pair(mode == 'crash_pair')
            returncode = details['producer']['returncode'] or details['importer']['returncode']
        else:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--child', mode],
                                    capture_output=True, text=True, timeout=20)
            details = {'stdout': result.stdout, 'stderr': result.stderr}
            returncode = result.returncode
        after = inventory()
        record['cycles'].append({'mode': mode, 'returncode': returncode, 'details': details,
                                 'created': {key: after[key] for key in after.keys() - before.keys()},
                                 'removed': sorted(before.keys() - after.keys())})
        (args.stage / 'result.json').write_text(json.dumps(record, indent=2) + '\n')
        if returncode:
            raise RuntimeError('driver diagnostic failed; inspect result.json')
    record['after'] = inventory()
    (args.stage / 'result.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps({'before_files': len(record['before']), 'after_files': len(record['after']),
                      'created_per_cycle': [len(item['created']) for item in record['cycles']]}))


if __name__ == '__main__':
    main()
