"""Move small Tensor descriptors through the existing generated native owner."""


def _event_bytes(value):
    # bytes() alone accepts bool and arbitrary __index__ objects. Match the
    # native binding's PyLong/not-bool contract, including int subclasses.
    result = bytearray()
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool):
            raise TypeError('integer field requires int')
        item = int.__index__(item)
        if not 0 <= item <= 255:
            raise ValueError('integer exceeds field range')
        result.append(item)
    return result


def fill_descriptor(message, descriptor):
    """Resize on the worker before caller-thread writes and scoped metadata views."""
    owner = message._owner
    generation = owner.snapshot['generation']
    pending = [((), (), descriptor)]
    writes = []
    while pending:
        path, names, value = pending.pop()
        info = owner.info(path, generation)
        kind = info['kind']
        if kind == 'message':
            members = info['members']
            if set(value) != set(members):
                raise ValueError('GPU descriptor fields do not match the installed schema')
            pending.extend(((*path, index), (*names, name), value[name])
                           for index, name in enumerate(members))
            continue
        if kind == 'scalar':
            writes.append((path, False, value))
            continue
        if kind == 'string':
            value = value.encode('utf-8')
        if kind in ('sequence', 'string'):
            owner.resize(path, generation, len(value))
            generation = owner.snapshot['generation']
        elif kind == 'array' and len(value) != info['size']:
            raise ValueError('GPU descriptor fixed array has incorrect length')
        if names == ('core', 'event_handle') and kind == 'array':
            if info['size'] != 64:
                raise ValueError('GPU descriptor event handle has incorrect length')
            writes.append((path, True, _event_bytes(value)))
        elif kind == 'string':
            writes.append((path, True, value))
        elif names in (('shape',), ('strides',)) and kind == 'sequence':
            # These two fields are int64[] in GpuTensor. Other sequences may
            # contain messages/strings and must retain structural traversal.
            writes.extend(((*path, index), False, item) for index, item in enumerate(value))
        else:
            pending.extend(((*path, index), (*names, index), item)
                           for index, item in enumerate(value))
    # Every path uses the final structural generation, after all resizes.
    for path, bulk, value in writes:
        if bulk:
            with owner.buffer(path, generation) as view:
                view[:] = value
        else:
            owner.write(path, generation, value)


def _read_field(owner, path, generation, names=()):
    # Keep recursion at module scope: a local recursive closure would retain its
    # CPU descriptor owner until cyclic GC, independently of callback lifetime.
    info = owner.info(path, generation)
    generation = info['generation']
    kind = info['kind']
    if kind == 'message':
        return {name: _read_field(owner, (*path, index), generation, (*names, name))
                for index, name in enumerate(info['members'])}
    if kind == 'scalar':
        return owner.read(path, generation)
    if kind == 'string':
        return owner.text_bytes(path, generation).decode('utf-8')
    if names == ('core', 'event_handle') and kind == 'array':
        if info['size'] != 64:
            raise ValueError('GPU descriptor event handle has incorrect length')
        with owner.buffer(path, generation) as view:
            return list(view)
    return [_read_field(owner, (*path, index), generation, (*names, index))
            for index in range(info['size'])]


def read_descriptor(message):
    """Copy descriptor metadata only; GPU payload is neither mapped nor copied here."""
    return _read_field(message._owner, (), 0)
