"""Launch integration keeps executable ownership and bounds all cleanup attempts."""

import os
from pathlib import Path
import signal
from types import SimpleNamespace

import pytest

from hermos_runtime.supervision import group, isolated, run


def test_installed_executable_is_resolved_in_peer_overlay_without_ros2_wrapper(tmp_path, monkeypatch):
    executable = tmp_path / 'lib/camera_pkg/camera_peer'
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o755)
    marker = tmp_path / 'share/ament_index/resource_index/packages/camera_pkg'
    marker.parent.mkdir(parents=True)
    marker.touch()
    monkeypatch.setenv('AMENT_PREFIX_PATH', str(tmp_path / 'other-overlay'))
    command = dict(package='camera_pkg', argv=['camera_peer', 'argument'],
                   env={'AMENT_PREFIX_PATH': str(tmp_path)})
    assert group.command_argv(command) == [
        str(executable), 'argument']
    executable.chmod(0o644)
    with pytest.raises(ValueError, match='executable'):
        group.command_argv(command)


@pytest.mark.parametrize('package,executable', [('pkg', '../peer'), ('../pkg', 'peer'), ('pkg', '/bin/sh')])
def test_package_resolution_rejects_paths_before_spawning(package, executable):
    with pytest.raises(ValueError):
        group.validate([dict(package=package, argv=[executable])], 10, 5)


def test_launch_budget_covers_startup_and_two_complete_stop_attempts():
    startup, stop = 10, 5
    # Each stop can write control, wait for the wrapper, then wait for the worker.
    one_worker_wait = startup + 4 * stop + 2
    pending_start = stop + startup + stop
    cleanup = 2 * (stop + 2 * one_worker_wait)
    assert isolated.supervisor_shutdown_timeout(startup, stop) > pending_start + cleanup


def test_parent_death_registration_closes_race_after_prctl(monkeypatch):
    calls = []
    parents = iter([123, 1])
    monkeypatch.setattr(os, 'getppid', lambda: next(parents))
    monkeypatch.setattr(run.ctypes, 'CDLL', lambda *args, **kwargs: SimpleNamespace(
        prctl=lambda *args: calls.append(args) or 0))
    monkeypatch.setattr(os, 'kill', lambda pid, number: calls.append((pid, number)))
    run.watch_parent(123)
    assert calls == [(1, signal.SIGTERM, 0, 0, 0), (os.getpid(), signal.SIGTERM)]


def test_missing_launch_parent_is_rejected_before_epoch(monkeypatch):
    monkeypatch.setattr(os, 'getppid', lambda: 1)
    with pytest.raises(RuntimeError, match='parent'):
        run.watch_parent(123)
