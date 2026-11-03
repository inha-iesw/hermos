"""Descriptor values and failure cleanup, separate from native transport timing."""

import pytest

from rclpy_hermos_gpu.endpoint.metadata import fill_descriptor, read_descriptor


def test_metadata_round_trip_preserves_every_field_and_uses_scoped_bulk_views(metadata_case):
    message, descriptor = metadata_case
    fill_descriptor(message, descriptor)
    owner = message._owner
    assert owner.values[(0, 0)] == '/tmp/cuda-é.sock'.encode('utf-8')
    assert owner.values[(0, 1)] == bytes(range(64))
    assert owner.exports == 0
    assert not any(event[0] == 'worker-write' for event in owner.events)
    assert [event for event in owner.events if event[0] == 'buffer'] == [
        ('buffer', (0, 1)), ('buffer', (0, 0))]
    assert sum(event[0] == 'resize' for event in owner.events) == 3
    # This is an algorithmic call-pattern assertion, not a measured worker count.
    first_write = next(i for i, event in enumerate(owner.events) if event[0] in ('write', 'buffer'))
    assert all(event[0] != 'resize' for event in owner.events[first_write:])
    owner.events.clear()
    assert read_descriptor(message) == descriptor
    assert owner.exports == 0
    assert ('buffer', (0, 1)) in owner.events
    assert not any(event[0] == 'read' and event[1][:2] == (0, 1) for event in owner.events)


@pytest.mark.parametrize('shape,strides,path', [([], [], ''), ([0], [], '/tmp/한.sock')])
def test_empty_fields_and_utf8_resize_by_byte_length(metadata_case, shape, strides, path):
    message, descriptor = metadata_case
    descriptor.update(shape=shape, strides=strides)
    descriptor['core']['vmm_socket_path'] = path
    fill_descriptor(message, descriptor)
    assert message._owner.values[(0, 0)] == path.encode('utf-8')
    assert read_descriptor(message) == descriptor


@pytest.mark.parametrize('size', [63, 65])
def test_fixed_event_handle_length_is_rejected(metadata_case, size):
    message, descriptor = metadata_case
    descriptor['core']['event_handle'] = [0] * size
    with pytest.raises(ValueError, match='length'):
        fill_descriptor(message, descriptor)
    assert message._owner.exports == 0


class IndexOnly:
    def __index__(self):
        return 1


@pytest.mark.parametrize('value,error', [(True, TypeError), (IndexOnly(), TypeError),
                                        (-1, ValueError), (256, ValueError)])
def test_bulk_event_bytes_preserve_native_integer_validation(metadata_case, value, error):
    message, descriptor = metadata_case
    descriptor['core']['event_handle'][7] = value
    with pytest.raises(error):
        fill_descriptor(message, descriptor)
    assert message._owner.exports == 0


def test_event_byte_int_subclass_keeps_native_pylong_value(metadata_case):
    class Integer(int):
        def __index__(self):
            return 0

        def __int__(self):
            return 0

    message, descriptor = metadata_case
    descriptor['core']['event_handle'][7] = Integer(17)
    fill_descriptor(message, descriptor)
    assert message._owner.values[(0, 1)][7] == 17


@pytest.mark.parametrize('path', [(0, 0), (0, 1)])
def test_assignment_failure_releases_every_acquired_view(metadata_case, path):
    message, descriptor = metadata_case
    message._owner.fail_buffer = path
    with pytest.raises(TypeError, match='read-only'):
        fill_descriptor(message, descriptor)
    assert message._owner.exports == 0


@pytest.mark.parametrize('field,value,error', [('dtype_bits', True, TypeError),
                                              ('dtype_lanes', 65536, ValueError),
                                              ('shape', [2**63], ValueError)])
def test_direct_scalar_writes_keep_native_type_and_range_checks(metadata_case, field, value, error):
    message, descriptor = metadata_case
    descriptor[field] = value
    with pytest.raises(error):
        fill_descriptor(message, descriptor)
    assert message._owner.exports == 0


def test_unknown_schema_field_is_rejected_before_filling(metadata_case):
    message, descriptor = metadata_case
    descriptor['unrecognized'] = 1
    with pytest.raises(ValueError, match='schema'):
        fill_descriptor(message, descriptor)
    assert not message._owner.filling


def test_other_sequences_keep_nested_structure_traversal():
    from types import SimpleNamespace

    events = []

    class NestedOwner:
        generation = 0

        @property
        def snapshot(self):
            return {'generation': self.generation}

        def info(self, path, generation):
            assert not path or generation == self.generation
            fields = {(): ('message', ('children',)), (0,): ('sequence', ()),
                      (0, 0): ('message', ('values',)), (0, 0, 0): ('sequence', ()),
                      (0, 0, 0, 0): ('scalar', ())}
            kind, members = fields[path]
            return dict(kind=kind, members=members, generation=self.generation)

        def resize(self, path, generation, size):
            assert generation == self.generation
            assert not any(event[0] == 'write' for event in events)
            events.append(('resize', path, size))
            self.generation += 1

        def write(self, path, generation, value):
            assert generation == self.generation
            events.append(('write', path, value))

    fill_descriptor(SimpleNamespace(_owner=NestedOwner()), {'children': [{'values': [7]}]})
    assert events == [('resize', (0,), 1), ('resize', (0, 0, 0), 1),
                      ('write', (0, 0, 0, 0), 7)]
