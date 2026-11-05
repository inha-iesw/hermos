"""Relocated worker discovery with the bootstrap's PATH-only, -E environment."""

import ctypes
import json
from pathlib import Path
import shutil
import subprocess

import pytest


SOURCE = Path(__file__).resolve().parents[2] / 'scripts/namespace_worker.py'


@pytest.fixture(params=['isolated/hermos_runtime', 'merged'])
def installation(tmp_path, request):
    prefix = tmp_path / request.param
    entry = prefix / 'lib/hermos_runtime/namespace_worker.py'
    entry.parent.mkdir(parents=True)
    shutil.copyfile(SOURCE, entry)
    share = prefix / 'share/hermos_runtime'
    share.mkdir(parents=True)
    python_dir = 'lib/python3/site-packages'
    (share / 'supervision.json').write_text(json.dumps(
        dict(version=1, python_dir=python_dir)))
    package = prefix / python_dir / 'hermos_runtime/supervision'
    package.mkdir(parents=True)
    (package.parent / '__init__.py').write_text('')
    (package / '__init__.py').write_text('')
    (package / 'namespace_worker.py').write_text(
        'import json, sys\n'
        'def main():\n'
        '    print(json.dumps({"file": __file__, "args": sys.argv[1:]}))\n'
        '    return 0\n')
    return prefix, entry, share


def launch(entry, cwd):
    def drop():
        if ctypes.CDLL(None).prctl(38, 1, 0, 0, 0) != 0:
            raise RuntimeError('cannot set NoNewPrivs')
    return subprocess.run(['/usr/bin/python3', '-B', '-E', '-s', str(entry), 'stage', '{}'],
                          cwd=cwd, env={'PATH': '/usr/bin:/bin'}, preexec_fn=drop,
                          capture_output=True, text=True, timeout=10)


def test_relocated_entry_ignores_cwd_and_finds_only_installed_package(installation, tmp_path):
    prefix, entry, _ = installation
    # A caller's working directory must never supply the worker package.
    (tmp_path / 'hermos_runtime.py').write_text('raise RuntimeError("cwd imported")')
    relocated = prefix.with_name(prefix.name + '-relocated')
    prefix.rename(relocated)
    result = launch(relocated / entry.relative_to(prefix), tmp_path)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output['file'].startswith(str(relocated))
    assert output['args'] == ['stage', '{}']


@pytest.mark.parametrize('python_dir', ['/tmp', '../../escape', '.', ''])
def test_entry_rejects_manifest_paths_outside_installation(installation, tmp_path, python_dir):
    _, entry, share = installation
    (share / 'supervision.json').write_text(json.dumps(dict(version=1, python_dir=python_dir)))
    result = launch(entry, tmp_path)
    assert result.returncode != 0
    assert 'Python install path' in result.stderr


def test_entry_refuses_import_before_privilege_drop(installation, tmp_path):
    _, entry, _ = installation
    result = subprocess.run(['/usr/bin/python3', '-B', '-E', '-s', str(entry)],
                            cwd=tmp_path, env={'PATH': '/usr/bin:/bin'},
                            capture_output=True, text=True, timeout=10)
    # This process has not passed through the bootstrap's NoNewPrivs handoff.
    status = Path('/proc/self/status').read_text()
    if 'NoNewPrivs:\t0' not in status:
        pytest.skip('test parent already has NoNewPrivs')
    assert result.returncode != 0
    assert 'privilege drop' in result.stderr
