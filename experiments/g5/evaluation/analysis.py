"""Loss, pacing and resource perspectives on validated G5 timestamp records."""

import math

from experiments.g5.cpu_latency.analysis import analyze, _distribution


def evaluate(publisher, subscriber, *, warmup, samples, period_ns):
    """Recompute delivery first; a period budget is a diagnostic, not a deadline SLA."""
    if type(period_ns) is not int or period_ns <= 0:
        raise ValueError('period_ns must be a positive integer')
    result = analyze(publisher, subscriber, warmup=warmup, samples=samples)
    sent = {row[0]: row[1:] for row in publisher['samples'] if row[0] >= warmup}
    received = {row[0]: row[1:] for row in subscriber['samples'] if row[0] >= warmup}
    missing = sorted(set(sent) - set(received))
    longest = current = 0
    for sequence in range(warmup, warmup + samples):
        current = current + 1 if sequence not in received else 0
        longest = max(longest, current)
    starts = [sent[sequence][0] for sequence in sorted(sent)]
    gaps = [after - before for before, after in zip(starts, starts[1:])]
    if any(gap < 0 for gap in gaps):
        raise ValueError('publisher sequence timestamps moved backwards')
    chronological = sorted(received, key=lambda seq: received[seq][0])
    high_water = -1
    reordered = 0
    for sequence in chronological:
        reordered += sequence < high_water
        high_water = max(high_water, sequence)
    callbacks = [received[seq][0] for seq in chronological]
    completion = [received[seq][1] - sent[seq][0] for seq in received]
    within = sum(value <= period_ns for value in completion)
    result.update(
        missing_sequences=missing, longest_loss_burst=longest,
        reordered_receipts=reordered,
        fill_ns=_distribution([row[1] - row[0] for row in sent.values()]),
        consumer_ns=_distribution([row[1] - row[0] for row in received.values()]),
        completion_ns=_distribution(completion),
        sender_gap_ns=_distribution(gaps),
        sender_drift_ns=starts[-1] - starts[0] - (samples - 1) * period_ns,
        observed_send_hz=(samples - 1) * 1e9 / (starts[-1] - starts[0])
        if starts[-1] > starts[0] else None,
        callback_gap_ns=_distribution([b - a for a, b in zip(callbacks, callbacks[1:])]),
        consumer_within_period=within,
        consumer_period_exceedances=len(completion) - within,
        period_budget_unsatisfied=samples - within,
        publisher_resources=resource_window(publisher, warmup=warmup),
        subscriber_resources=resource_window(subscriber, warmup=warmup))
    return result


def resource_window(record, *, warmup):
    """Normalize only observed rows inside an explicitly identified counter interval.

    Old finish snapshots include delivery drain. New end snapshots may precede
    late/reordered receipts; their completeness is retained, never inferred from
    the final total. These are process costs, not isolated function timings.
    """
    before = record.get('resources_after_warmup')
    bounded = 'resources_measurement_end' in record
    after = record.get('resources_measurement_end' if bounded else 'resources_after')
    if before is None or after is None or 'warmup_snapshot_sequence' not in record:
        return {'available': False, 'reason': 'resource boundary unavailable'}
    start_usage = before.get('post_collection_usage', before)
    rows = record['samples']
    start = record.get('warmup_snapshot_count')
    if start is None:
        start = next((i + 1 for i, row in enumerate(rows)
                      if row[0] == record['warmup_snapshot_sequence']), None)
    end = record.get('measurement_end_snapshot_count') if bounded else len(rows)
    if (type(start) is not int or type(end) is not int or
            not 0 <= start <= end <= len(rows)):
        raise ValueError('invalid resource row boundary')
    count = end - start
    measured = sum(row[0] >= warmup for row in rows[start:end])
    keys = ('cpu_user_seconds', 'cpu_system_seconds', 'minor_page_faults',
            'major_page_faults', 'voluntary_context_switches', 'involuntary_context_switches')
    deltas = {}
    for key in keys:
        for boundary in (start_usage, after):
            if key in boundary and (type(boundary[key]) not in (int, float) or
                                    not math.isfinite(boundary[key]) or boundary[key] < 0):
                raise ValueError(f'invalid resource counter: {key}')
        delta = after[key] - start_usage[key] if key in start_usage and key in after else None
        if delta is not None and delta < 0:
            raise ValueError(f'resource counter decreased: {key}')
        deltas[key] = delta
    result = dict(available=True, observed_rows=count, measured_rows=measured,
                  late_warmup_rows=count - measured,
                  scope='post-warmup through final-ID observation' if bounded else
                  'post-warmup through finish/drain',
                  complete_window=record.get('measurement_end_complete') if bounded else None,
                  excludes_warmup_collection='post_collection_usage' in before,
                  counter_deltas=deltas)
    result.update({f'{key}_per_observed_row': delta / count if count and delta is not None else None
                   for key, delta in deltas.items()})
    cpu = [deltas[key] for key in ('cpu_user_seconds', 'cpu_system_seconds')]
    result['cpu_seconds_per_observed_row'] = sum(cpu) / count if count and None not in cpu else None
    for boundary, snapshot in (('start', before), ('end', after)):
        result[f'{boundary}_memory'] = snapshot.get('memory')
        result[f'{boundary}_collection_ns'] = [snapshot.get('collection_started_ns'),
                                               snapshot.get('collection_finished_ns')]
    return result


def compare(current, baseline):
    """Require complete paired evidence; preserve signed additional latency."""
    if not current['complete'] or not baseline['complete']:
        return dict(valid=False, reason='receipt loss or invalid chain')
    if current.get('resource_diagnostics') or baseline.get('resource_diagnostics'):
        return dict(valid=False, reason='resource-diagnostic timing')
    if current.get('timing_diagnostics') or baseline.get('timing_diagnostics'):
        return dict(valid=False, reason='cost-diagnostic timing')
    left, right = current['completion_ns'], baseline['completion_ns']
    if not left or not right or any(not right[key] for key in ('mean', 'p99')):
        return dict(valid=False, reason='unavailable or zero baseline')
    return dict(valid=True, **{f'{key}_change_percent': 100 * (left[key] / right[key] - 1)
                              for key in ('mean', 'p99')})
