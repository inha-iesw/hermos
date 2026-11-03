"""Reject unexpected loaded artifacts and unsettled GPU ownership before statistics."""

from copy import deepcopy
from pathlib import Path

import pytest

from experiments.g5.gpu_fanout.run import CORE_PREFIX, GPU_PREFIX, PREFIX, validate_record


def record(transport):
    case = {'transport': transport}
    paths = [PREFIX / 'g5_benchmark_msgs/lib/libg5_benchmark_msgs__rosidl_typesupport_c.so']
    if transport == 'hermos':
        paths += [PREFIX / 'rclpy_hermos/_native.test.so',
                  PREFIX / 'hermoslib/lib/libhermos.so',
                  PREFIX / 'hermoslib/lib/libhermos_heaphook.so',
                  CORE_PREFIX / 'rclpy_hermos_gpu_msgs/_types.test.so',
                  GPU_PREFIX / 'rclpy_hermos_gpu/_native.test.so']
    return case, dict(config=deepcopy(case), loaded_objects=[str(path) for path in paths],
                      runtime_after={'active_owners': 0, 'pending_owners': 0})


@pytest.mark.parametrize('transport', ['vanilla', 'hermos'])
def test_expected_production_mappings_are_accepted(transport):
    case, raw = record(transport)
    validate_record(raw, case)


def test_changed_peer_config_is_rejected():
    case, raw = record('vanilla')
    raw['config']['unexpected'] = True
    with pytest.raises(ValueError, match='config'):
        validate_record(raw, case)


def test_foreign_library_is_rejected():
    case, raw = record('vanilla')
    raw['loaded_objects'].append('/tmp/other/libhermos.so')
    with pytest.raises(RuntimeError, match='non-production'):
        validate_record(raw, case)


def test_stock_dds_peer_must_not_load_the_shared_heap_hook():
    case, raw = record('vanilla')
    raw['loaded_objects'].append(str(PREFIX / 'hermoslib/lib/libhermos_heaphook.so'))
    with pytest.raises(RuntimeError, match='must not load'):
        validate_record(raw, case)


@pytest.mark.parametrize('counter', ['active_owners', 'pending_owners'])
def test_unsettled_gpu_owners_reject_measurement(counter):
    case, raw = record('hermos')
    raw['runtime_after'][counter] = 1
    with pytest.raises(RuntimeError, match='owners did not settle'):
        validate_record(raw, case)


def test_cpu_native_module_does_not_substitute_for_missing_gpu_native_module():
    case, raw = record('hermos')
    raw['loaded_objects'] = [path for path in raw['loaded_objects']
                             if not Path(path).is_relative_to(GPU_PREFIX)]
    with pytest.raises(RuntimeError, match='GPU production native'):
        validate_record(raw, case)
