"""Fixed CPU experiment settings and independent process-success checks."""

import itertools


TYPES = {1024: 'Fixed1K', 4096: 'Fixed4K', 65536: 'Fixed64K',
         1048576: 'Fixed1M', 4194304: 'Fixed4M'}
RMWS = ('rmw_cyclonedds_cpp', 'rmw_fastrtps_cpp')


def validate(value):
    """Reject malformed or unmatched cases before creating a device epoch."""
    choices = dict(sizes=TYPES, layouts=('static', 'dynamic'), rmws=RMWS,
                   transports=('dds', 'hermos'))
    for key, allowed in choices.items():
        items = value.get(key)
        if (not isinstance(items, list) or not items or
                any(item not in allowed for item in items) or
                len(items) != len(set(items))):
            raise ValueError(f'invalid {key}')
    for key in ('samples', 'warmup', 'period_ns', 'depth'):
        item = value.get(key)
        if type(item) is not int or item < (0 if key == 'warmup' else 1):
            raise ValueError(f'invalid {key}')
    if type(value.get('full_check')) is not bool:
        raise ValueError('invalid full_check')


def cases(protocol):
    """Return the full declared matrix; retain its deterministic ordering."""
    validate(protocol)
    result = []
    for size, layout, rmw, transport in itertools.product(
            protocol['sizes'], protocol['layouts'], protocol['rmws'],
            protocol['transports']):
        name = f'{layout}-{size}-{rmw}-{transport}'
        result.append(dict(name=name, size=size, layout=layout, rmw=rmw,
                           transport=transport,
                           type=TYPES[size] if layout == 'static' else 'Dynamic'))
    return result
