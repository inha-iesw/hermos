"""Peer environment assembly preserves user settings and rejects conflicts."""

from pathlib import Path

import pytest

from hermos_runtime.environment import Installation, InstallationError, peer_environment


@pytest.fixture
def installed(tmp_path):
    hook = tmp_path / 'libhermos_heaphook.so'
    library = tmp_path / 'libhermos.so'
    hook.touch()
    library.touch()
    return Installation(hook, library, Path('/dev/hermos_rclpy'), {}, {})


def test_adds_peer_only_environment_and_preserves_other_preloads(installed, tmp_path):
    extra = tmp_path / 'libtrace.so'
    extra.touch()
    original = dict(LD_PRELOAD=str(extra), LD_LIBRARY_PATH='/opt/ros/humble/lib',
                    ROS_DOMAIN_ID='27', APP_VALUE='공백 value')
    result = peer_environment(installed, original)
    assert result['LD_PRELOAD'].split(':') == [str(installed.heaphook), str(extra)]
    assert result['HERMOS_DEVICE_PATH'] == '/dev/hermos_rclpy'
    assert result['HERMOS_BRIDGE_MODE'] == 'off'
    assert result['HERMOS_NO_DISCOVERY_AGENT'] == '1'
    assert result['ROS_DOMAIN_ID'] == '27'
    assert result['APP_VALUE'] == '공백 value'
    assert original['LD_PRELOAD'] == str(extra)
    assert 'HERMOS_DEVICE_PATH' not in original


def test_reuses_matching_preload_once(installed):
    result = peer_environment(installed, dict(LD_PRELOAD=str(installed.heaphook)))
    assert result['LD_PRELOAD'] == str(installed.heaphook)


def test_normalizes_differently_named_heaphook_alias(installed, tmp_path):
    alias = tmp_path / 'myhook.so'
    alias.symlink_to(installed.heaphook)
    result = peer_environment(installed, dict(LD_PRELOAD=str(alias)))
    assert result['LD_PRELOAD'] == str(installed.heaphook)


@pytest.mark.parametrize('entry', ['libhermos_heaphook.so',
    '/elsewhere/libhermos_heaphook.so', 'libjemalloc.so', 'libtcmalloc.so'])
def test_rejects_conflicting_or_ambiguous_allocator(installed, entry):
    with pytest.raises(InstallationError, match='preload|allocator'):
        peer_environment(installed, dict(LD_PRELOAD=entry))


def test_rejects_multiple_heaphooks(installed):
    with pytest.raises(InstallationError, match='preload'):
        peer_environment(installed, dict(LD_PRELOAD=f'{installed.heaphook}:{installed.heaphook}'))
