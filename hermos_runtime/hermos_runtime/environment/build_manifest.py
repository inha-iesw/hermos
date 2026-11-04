"""Build-time only: bind installed CPU binaries to their source and adapter ABI."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sysconfig


def source_identity(root):
    """Fingerprint relevant outer sources, including uncommitted implementation."""
    digest = hashlib.sha256()
    for name in ('rclpy_zc', 'rclpy_hermos_codegen', 'rclpy_hermos_adapters',
                 'hermos_runtime', 'hermos_launch'):
        for path in sorted((root / name).rglob('*')):
            if not path.is_file() or any(part in {'__pycache__', '.pytest_cache', '.git'}
                                        for part in path.parts) or path.suffix == '.pyc':
                continue
            digest.update(str(path.relative_to(root)).encode() + b'\0')
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                            text=True, capture_output=True, check=False)
    return dict(revision=result.stdout.strip() if result.returncode == 0 else None,
                tree_sha256=digest.hexdigest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('backend-prefix', 'native-prefix', 'python-dir', 'abi', 'source-root', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    version, tag = args.abi.read_text().split('\n', 1)
    artifacts = {}
    for role, package, prefix, relative in (
        ('library', 'hermoslib', args.backend_prefix, Path('lib/libhermos.so')),
        ('heaphook', 'hermoslib', args.backend_prefix, Path('lib/libhermos_heaphook.so')),
        ('registry', 'rclpy_hermos', args.native_prefix, Path('lib/librclpy_hermos_types.so')),
        ('native', 'rclpy_hermos', args.native_prefix,
         args.python_dir / 'rclpy_hermos' / ('_native' + sysconfig.get_config_var('EXT_SUFFIX'))),
    ):
        artifacts[role] = dict(package=package, path=str(relative),
            sha256=hashlib.sha256((prefix / relative).read_bytes()).hexdigest())
    backend = json.loads((args.backend_prefix / 'share/hermoslib/source-identity.json').read_text())
    manifest = dict(manifest_version=1, architecture=platform.machine(),
                    ros_distro=os.environ['ROS_DISTRO'], module='hermos_rclpy',
                    device='/dev/hermos_rclpy', build_kernel=platform.release(),
                    adapter_abi=int(version), build_abi=tag, artifacts=artifacts,
                    sources=dict(outer=source_identity(args.source_root.resolve()), backend=backend))
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')


if __name__ == '__main__':
    main()
