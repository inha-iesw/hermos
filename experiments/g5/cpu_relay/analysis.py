"""Validate a relay chain independently of its end-to-end delivery statistics."""

from experiments.g5.cpu_latency.analysis import analyze


def analyze_chain(source, relay, sink, *, warmup, samples):
    """Return separate hop measurements; any missing relay evidence invalidates a case.

    Relay rows are [sequence, callback, publish, publish_return]. They retain the
    source sequence. Copy/translation belongs to callback→publish, while the
    end-to-end metric remains the original source publish→sink callback.
    Missing records, including warm-up relay records, are explicit. Duplicates,
    impossible sequence paths and invalid timestamps raise ValueError.
    """
    if not isinstance(relay, dict) or not isinstance(relay.get('samples'), list):
        raise ValueError('relay samples must be timestamp rows')
    rows = relay['samples']
    for row in rows:
        if (not isinstance(row, (list, tuple)) or len(row) != 4 or
                any(type(value) is not int or value < 0 for value in row) or
                not row[1] <= row[2] <= row[3]):
            raise ValueError('invalid relay timestamp row')
    input_receipts = {'samples': [[row[0], row[1], row[3]] for row in rows]}
    first = analyze(source, input_receipts, warmup=warmup, samples=samples)
    end_to_end = analyze(source, sink, warmup=warmup, samples=samples)
    relayed = {row[0]: row for row in rows}
    for sequence, callback, _ in sink['samples']:
        if sequence not in relayed:
            raise ValueError(f'sink sequence {sequence} has no relay record')
        if callback < relayed[sequence][2]:
            raise ValueError(f'sink timestamp precedes relay publish for sequence {sequence}')
    missing = warmup + samples - len(rows)
    second = analyze(relay, sink, warmup=warmup, samples=samples) if missing == 0 else None
    first['source_fill_to_relay_return_ns'] = first.pop('fill_to_consumer_ns')
    if second is not None:
        second['relay_callback_to_sink_done_ns'] = second.pop('fill_to_consumer_ns')
    return dict(complete=first['complete'] and end_to_end['complete'] and missing == 0,
                missing_relay_records=missing, source_to_sink=end_to_end,
                source_to_relay=first, relay_to_sink=second)
