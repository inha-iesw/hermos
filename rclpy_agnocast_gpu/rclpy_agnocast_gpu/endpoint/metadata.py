"""Move small Tensor descriptors through the existing generated native owner."""


def _children(owner, path, generation, value):
    info = owner.info(path, generation)
    if info['kind'] == 'message':
        members = info['members']
        if set(value) != set(members):
            raise ValueError('GPU descriptor fields do not match the installed schema')
        return [((*path, index), value[name]) for index, name in enumerate(members)]
    if info['kind'] in ('array', 'sequence', 'string'):
        if info['kind'] == 'string':
            value = value.encode('utf-8')
        return [((*path, index), item) for index, item in enumerate(value)]
    return []


def fill_descriptor(message, descriptor):
    """Resize first, then write every scalar on the native metadata worker."""
    owner = message._owner
    pending = [((), descriptor)]
    while pending:
        path, value = pending.pop()
        generation = owner.snapshot['generation']
        info = owner.info(path, generation)
        if info['kind'] in ('sequence', 'string'):
            size = len(value.encode('utf-8')) if info['kind'] == 'string' else len(value)
            owner.resize(path, generation, size)
            generation = owner.snapshot['generation']
        elif info['kind'] == 'array' and len(value) != info['size']:
            raise ValueError('GPU descriptor fixed array has incorrect length')
        pending.extend(_children(owner, path, generation, value))
    generation = owner.snapshot['generation']
    pending = [((), descriptor)]
    while pending:
        path, value = pending.pop()
        info = owner.info(path, generation)
        if info['kind'] == 'scalar':
            owner._write_on_worker(path, generation, value)
        else:
            pending.extend(_children(owner, path, generation, value))


def _read_field(owner, path, generation):
    # Keep recursion at module scope: a local recursive closure would retain its
    # CPU descriptor owner until cyclic GC, independently of callback lifetime.
    info = owner.info(path, generation)
    generation = info['generation']
    kind = info['kind']
    if kind == 'message':
        return {name: _read_field(owner, (*path, index), generation)
                for index, name in enumerate(info['members'])}
    if kind == 'scalar':
        return owner.read(path, generation)
    if kind == 'string':
        return owner.text_bytes(path, generation).decode('utf-8')
    return [_read_field(owner, (*path, index), generation) for index in range(info['size'])]


def read_descriptor(message):
    """Copy descriptor metadata only; GPU payload is neither mapped nor copied here."""
    return _read_field(message._owner, (), 0)
