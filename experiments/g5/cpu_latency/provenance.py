"""Validate production inputs before measuring and retain their exact identity."""

import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[3]
PREFIX = ROOT / 'install/hermos/g5/production'
SOURCE_SUFFIXES = {'.py', '.sh', '.cpp', '.cc', '.c', '.hpp', '.h', '.rs', '.cmake',
                   '.msg', '.in', '.toml'}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_release(cache):
    values = dict(line.split('=', 1) for line in cache.read_text().splitlines()
                  if line and not line.startswith(('#', '//')) and '=' in line)
    if (values.get('CMAKE_BUILD_TYPE:STRING') != 'Release' or
            values.get('BUILD_TESTING:BOOL') != 'OFF'):
        raise RuntimeError(f'{cache} is not Release/BUILD_TESTING=OFF')


def hash_artifacts(prefix):
    return {str(path.relative_to(ROOT)): digest(path) for path in prefix.rglob('*')
            if path.is_file() and ('.so' in path.name or path.suffix in ('.a', '.py', '.json'))}


def hash_sources(directories):
    sources = {}
    for directory in directories:
        for path in (ROOT / directory).rglob('*'):
            if any(part in ('target', '__pycache__', '.pytest_cache', '.git')
                   for part in path.parts):
                continue
            if path.is_file() and (path.suffix in SOURCE_SUFFIXES or
                                   path.name in ('CMakeLists.txt', 'Cargo.lock', 'package.xml')):
                sources[str(path.relative_to(ROOT))] = digest(path)
    return sources


def mapped_objects():
    """List project/legacy libraries so mixed prefixes fail provenance validation."""
    return sorted({parts[5] for line in Path('/proc/self/maps').read_text().splitlines()
                   if len(parts := line.split(maxsplit=5)) == 6 and
                   '.so' in Path(parts[5]).name and
                   any(key in parts[5] for key in ('hermos', 'herm0s', 'agnocast', 'g5_benchmark', 'cuda_ipc'))})


def require_uninstrumented(module):
    if any(name.lower().startswith('_test') for name in dir(module)):
        raise RuntimeError('test-instrumented native module in benchmark')


def archive_sources(identity, output):
    """Preserve measured working-tree inputs that may not yet have a commit."""
    with tarfile.open(output, 'x:gz') as archive:
        for name, expected in identity['source_sha256'].items():
            path = ROOT / name
            if digest(path) != expected:
                raise RuntimeError(f'source changed before archiving: {name}')
            archive.add(path, arcname=name, recursive=False)


def capture():
    """Fail closed on instrumented or missing artifacts; do not initialize ROS."""
    for package in ('hermoslib', 'rclpy_hermos', 'g5_benchmark_adapters', 'g5_benchmark_msgs'):
        cache = ROOT / 'build/hermos/g5/production' / package / 'CMakeCache.txt'
        require_release(cache)
    # Include transitive project dependencies such as hermos_cie_config_msgs,
    # not only the four packages whose production build options are checked.
    artifacts = hash_artifacts(PREFIX)
    fingerprints = list((ROOT / 'build/hermos/g5/heaphook/release/.fingerprint').glob(
        'hermos_heaphook-*/lib-hermos_heaphook.json'))
    if len(fingerprints) != 1 or json.loads(fingerprints[0].read_text())['features'] != '[]':
        raise RuntimeError('heaphook must have no observation feature')
    hook = PREFIX / 'hermoslib/lib/libhermos_heaphook.so'
    symbols = subprocess.check_output(['nm', '-D', str(hook)], text=True)
    if 'hermos_test_' in symbols:
        raise RuntimeError('instrumented heaphook symbols')
    if digest(hook) != digest(ROOT / 'build/hermos/g5/heaphook/release/libhermos_heaphook.so'):
        raise RuntimeError('heaphook install differs from feature-checked artifact')
    sources = hash_sources(('experiments/g5', 'rclpy_zc', 'rclpy_hermos_codegen',
                            'phase4_gpu_core_probe/repair/recovery',
                            'phase4_gpu_core_probe/repair/session',
                            'hermos/hermos_heaphook', 'hermos/src/hermoslib'))
    dds_keys = ('CYCLONEDDS_URI', 'FASTRTPS_DEFAULT_PROFILES_FILE',
                'FASTDDS_DEFAULT_PROFILES_FILE', 'RMW_FASTRTPS_USE_QOS_FROM_XML',
                'RMW_FASTRTPS_PUBLICATION_MODE', 'SKIP_DEFAULT_XML')
    return dict(platform=platform.platform(), machine=platform.machine(),
                python=platform.python_version(),
                dds_environment={key: os.environ.get(key) for key in dds_keys},
                cpu_affinity=sorted(os.sched_getaffinity(0)), source_sha256=sources,
                artifact_sha256=artifacts,
                heaphook_fingerprint=json.loads(fingerprints[0].read_text()),
                os_release=Path('/etc/os-release').read_text(),
                hermos_head=subprocess.check_output(
                    ['git', 'rev-parse', 'HEAD'], cwd=ROOT / 'hermos', text=True).strip(),
                git_head=subprocess.check_output(
                    ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip())


def verify_peer(record, proposed, *, prefixes=None, required=None):
    """Reject accidentally loaded Debug/test libraries in real measurement peers."""
    # /proc/maps also lists the legitimate shared heap /dev/shm/hermos@PID.
    # Artifact provenance applies to shared libraries, not payload mappings.
    paths = [Path(path) for path in record['loaded_objects'] if '.so' in Path(path).name]
    prefixes = (PREFIX,) if prefixes is None else prefixes
    if not paths or any(not any(path.is_relative_to(prefix) for prefix in prefixes)
                        for path in paths):
        raise RuntimeError(f'peer loaded non-production artifacts: {paths}')
    names = [path.name for path in paths]
    if not proposed and any(name == 'libhermos_heaphook.so' for name in names):
        raise RuntimeError('DDS baseline must not load Hermos heaphook')
    if required is None:
        required = (('_native.', 'libhermos.so', 'libhermos_heaphook.so', '_types.')
                    if proposed else ())
    if not all(any(name.startswith(key) for name in names) for key in required):
        raise RuntimeError('peer production artifact inventory is incomplete')
