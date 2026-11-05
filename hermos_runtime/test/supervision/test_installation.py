"""Bind runtime imports to one installation before starting a namespace worker."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SOURCE = Path(__file__).resolve().parents[2]
PYTHON_DIR = 'lib/python3/site-packages'


def installation(prefix, *, symlinks=False):
    package = prefix / PYTHON_DIR / 'hermos_runtime'
    shutil.copytree(SOURCE / 'hermos_runtime', package,
                    copy_function=os.symlink if symlinks else shutil.copy2,
                    ignore=shutil.ignore_patterns('__pycache__'))
    share = prefix / 'share/hermos_runtime'
    share.mkdir(parents=True)
    (share / 'supervision.json').write_text(json.dumps(
        dict(version=1, python_dir=PYTHON_DIR)))
    marker = prefix / 'share/ament_index/resource_index/packages/hermos_runtime'
    marker.parent.mkdir(parents=True)
    marker.touch()
    entry = prefix / 'lib/hermos_runtime/hermos_supervisor'
    entry.parent.mkdir(parents=True)
    if symlinks:
        entry.symlink_to(SOURCE / 'scripts/hermos_supervisor')
    else:
        shutil.copyfile(SOURCE / 'scripts/hermos_supervisor', entry)
    return prefix


def invoke(imported, selected, *args, cwd):
    environment = {'PATH': '/usr/bin:/bin',
                   'PYTHONPATH': str(imported / PYTHON_DIR),
                   'AMENT_PREFIX_PATH': str(selected)}
    return subprocess.run([sys.executable, '-B', *args], env=environment, cwd=cwd,
                          capture_output=True, text=True, timeout=10)


CHECK = ('from hermos_runtime.supervision.installation import validate_installation; '
         'print(validate_installation())')


@pytest.mark.parametrize('layout', ['isolated/hermos_runtime', 'merged'])
def test_relocated_installation_binds_loaded_runtime_without_ament(tmp_path, layout):
    original = installation(tmp_path / layout)
    prefix = original.with_name(original.name + '-relocated')
    original.rename(prefix)
    code = CHECK + '; import sys; assert not any(k.startswith("ament_") for k in sys.modules)'
    result = invoke(prefix, prefix, '-c', code, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(prefix)


def test_symlink_install_uses_invoked_prefix_and_canonical_module_files(tmp_path):
    prefix = installation(tmp_path / 'symlink', symlinks=True)
    result = invoke(prefix, prefix, '-c', CHECK, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(prefix)
    entry = prefix / 'lib/hermos_runtime/hermos_supervisor'
    result = invoke(prefix, prefix, str(entry), '--help', cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'usage:' in result.stdout


def test_selected_overlay_cannot_use_another_imported_runtime(tmp_path):
    imported = installation(tmp_path / 'imported')
    selected = installation(tmp_path / 'selected')
    result = invoke(imported, selected, '-c', CHECK, cwd=tmp_path)
    assert result.returncode != 0
    assert 'mixed hermos_runtime installation' in result.stderr


def test_explicit_environment_selects_prefix_without_mutating_process_environment(tmp_path):
    prefix = installation(tmp_path / 'selected')
    code = ('import os\n'
            'from hermos_runtime.supervision.installation import validate_installation\n'
            'before = dict(os.environ)\n'
            f'print(validate_installation({{"AMENT_PREFIX_PATH": {str(prefix)!r}}}))\n'
            'assert dict(os.environ) == before\n')
    result = invoke(prefix, tmp_path / 'missing', '-c', code, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(prefix)


def test_epoch_rejects_selected_origin_mismatch_before_sudo(tmp_path):
    imported = installation(tmp_path / 'imported')
    selected = installation(tmp_path / 'selected')
    code = ('from hermos_runtime.supervision import isolated\n'
            'def forbidden(*args, **kwargs):\n'
            '    raise AssertionError("sudo reached")\n'
            'isolated.subprocess.Popen = forbidden\n'
            'isolated.trusted_bootstrap = lambda: "/unused-bootstrap"\n'
            f'epoch = isolated.IsolatedEpoch([{{"argv": ["unused"], "env": '
            '{"HERMOS_BRIDGE_MODE": "off", "HERMOS_NO_DISCOVERY_AGENT": "1"}}], '
            f'{str(tmp_path / "epoch")!r})\n'
            'epoch.start()\n')
    result = invoke(imported, selected, '-c', code, cwd=tmp_path)
    assert result.returncode != 0
    assert 'mixed hermos_runtime installation' in result.stderr
    assert 'sudo reached' not in result.stderr


def test_mixed_loaded_submodule_is_rejected_even_when_package_matches(tmp_path):
    prefix = installation(tmp_path / 'selected')
    other = installation(tmp_path / 'other')
    code = ('from hermos_runtime.supervision.installation import validate_installation\n'
            'import hermos_runtime.supervision\n'
            f'hermos_runtime.supervision.__path__.insert(0, {str(other / PYTHON_DIR / "hermos_runtime/supervision")!r})\n'
            'from hermos_runtime.supervision import group\n'
            'validate_installation()\n')
    result = invoke(prefix, prefix, '-c', code, cwd=tmp_path)
    assert result.returncode != 0
    assert 'mixed hermos_runtime installation' in result.stderr
    assert 'hermos_runtime.supervision.group' in result.stderr


def test_supervisor_entry_rejects_pythonpath_from_another_installation(tmp_path):
    invoked = installation(tmp_path / 'invoked')
    imported = installation(tmp_path / 'imported')
    entry = invoked / 'lib/hermos_runtime/hermos_supervisor'
    result = invoke(imported, invoked, str(entry), '--help', cwd=tmp_path)
    assert result.returncode != 0
    assert 'mixed hermos_runtime installation' in result.stderr


@pytest.mark.parametrize('python_dir', ['/tmp', '../../escape', '.', ''])
def test_invalid_manifest_python_path_is_rejected(tmp_path, python_dir):
    prefix = installation(tmp_path / 'installed')
    (prefix / 'share/hermos_runtime/supervision.json').write_text(json.dumps(
        dict(version=1, python_dir=python_dir)))
    result = invoke(prefix, prefix, '-c', CHECK, cwd=tmp_path)
    assert result.returncode != 0
    assert 'Python install path' in result.stderr
