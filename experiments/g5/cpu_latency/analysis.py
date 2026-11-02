"""Join peer-local timestamps without introducing a measured ACK round trip."""

import statistics


def _records(record, *, owner, width, total):
    if not isinstance(record, dict) or not isinstance(record.get('samples'), (list, tuple)):
        raise ValueError(f'{owner} samples must be a sequence of timestamp rows')
    indexed = {}
    for index, row in enumerate(record['samples']):
        if not isinstance(row, (list, tuple)) or len(row) != width:
            raise ValueError(f'{owner} row {index} must contain {width} integers')
        if any(type(value) is not int or value < 0 for value in row):
            raise ValueError(f'{owner} row {index} requires nonnegative integers')
        sequence = row[0]
        if sequence >= total:
            raise ValueError(f'{owner} sequence {sequence} was not scheduled')
        if sequence in indexed:
            raise ValueError(f'{owner} duplicate sequence {sequence}')
        if any(before > after for before, after in zip(row[1:], row[2:])):
            raise ValueError(f'{owner} timestamp order is invalid for sequence {sequence}')
        indexed[sequence] = tuple(row[1:])
    return indexed


def _distribution(values):
    if not values:
        return None
    ordered = sorted(values)

    def percentile(fraction):
        # Linear interpolation at (n - 1) * fraction, including the endpoints.
        position = (len(ordered) - 1) * fraction
        left = int(position)
        right = min(left + 1, len(ordered) - 1)
        return ordered[left] + (ordered[right] - ordered[left]) * (position - left)

    return {
        'count': len(values),
        'mean': statistics.fmean(values),
        'min': ordered[0],
        'max': ordered[-1],
        'variance': statistics.pvariance(values),
        'p50': percentile(0.50),
        'p99': percentile(0.99),
    }


def analyze(publisher: dict, subscriber: dict, *, warmup: int, samples: int) -> dict:
    """Validate and join same-host CLOCK_MONOTONIC peer evidence by sequence.

    Publisher rows contain ``[sequence, fill_start, publish, publish_return]``;
    subscriber rows contain ``[sequence, callback, consumer_done]``. Timestamps
    are nonnegative integer nanoseconds. Publisher evidence must contain every
    scheduled sequence, including warm-up. Missing subscriber receipts are loss,
    while duplicate, unscheduled or temporally inconsistent rows raise ValueError.
    A callback is allowed to begin before its publisher call returns.

    ``expected``, ``received``, ``lost`` and ``complete`` concern measured IDs only.
    Warm-up IDs are validated but excluded from all statistics. Publish-call cost
    includes every measured send, even when its receipt is missing. Other latency
    distributions include joined receipts only. Variance is population variance
    in squared nanoseconds; p50/p99 interpolate linearly at ``(n - 1) * q``.

    ``observation_window_ns`` spans the earliest and latest measured callbacks.
    ``observed_delivery_per_second`` divides the N-1 inter-receipt intervals by
    that window. Fewer than two receipts or a zero-duration window returns None
    for the rate. This paced observation is not a saturation throughput claim.
    With no measured receipts, receipt latency distributions and the window are
    None; loss counts and publish-call statistics remain available.
    """
    if type(warmup) is not int or warmup < 0:
        raise ValueError('warmup must be a nonnegative integer')
    if type(samples) is not int or samples <= 0:
        raise ValueError('samples must be a positive integer')
    total = warmup + samples
    sent = _records(publisher, owner='publisher', width=4, total=total)
    if set(sent) != set(range(total)):
        raise ValueError('publisher evidence must contain every scheduled sequence')
    received = _records(subscriber, owner='subscriber', width=3, total=total)
    for sequence, (callback, _) in received.items():
        if callback < sent[sequence][1]:
            raise ValueError(f'joined timestamp order is invalid for sequence {sequence}')

    measured = range(warmup, total)
    joined = sorted(sequence for sequence in received if sequence >= warmup)
    callbacks = [received[sequence][0] for sequence in joined]
    window = max(callbacks) - min(callbacks) if callbacks else None
    return {
        'expected': samples,
        'received': len(joined),
        'lost': samples - len(joined),
        'complete': len(joined) == samples,
        'publish_to_callback_ns': _distribution([
            received[sequence][0] - sent[sequence][1] for sequence in joined]),
        'fill_to_consumer_ns': _distribution([
            received[sequence][1] - sent[sequence][0] for sequence in joined]),
        'publish_call_ns': _distribution([
            sent[sequence][2] - sent[sequence][1] for sequence in measured]),
        'observation_window_ns': window,
        'observed_delivery_per_second': (len(joined) - 1) * 1e9 / window if window else None,
    }
