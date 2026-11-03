"""Per-sample diagnostic call sums; nested fill/resize intervals are not additive."""

from experiments.g5.cpu_latency.analysis import _distribution


def _export_remainder(events):
    total = [event for event in events if event['stage'] == 'tensor_export_total']
    children = [event for event in events if event['stage'] in ('descriptor_decode', 'map')]
    if (len(total) != 1 or len(children) != 2 or
            {event['stage'] for event in children} != {'descriptor_decode', 'map'} or
            any(event['failed'] for event in total + children)):
        return None
    outer = total[0]
    children.sort(key=lambda event: event['started_ns'])
    if (any(not outer['started_ns'] <= event['started_ns'] <= event['finished_ns'] <=
            outer['finished_ns'] for event in children) or
            children[0]['finished_ns'] > children[1]['started_ns']):
        return None
    return outer['finished_ns'] - outer['started_ns'] - sum(
        event['finished_ns'] - event['started_ns'] for event in children)


def summarize(records, *, warmup, samples):
    """Exclude warmup and aggregate each stage's calls within each observed sample."""
    seen, totals, calls, failures = set(), {}, {}, {}
    exclusive_export_unavailable = 0
    for record in records:
        sequence = record['sequence']
        if (type(sequence) is not int or not 0 <= sequence < warmup + samples or
                sequence in seen):
            raise ValueError('invalid or duplicate cost sequence')
        seen.add(sequence)
        durations = {}
        for event in record['events']:
            started, finished = event['started_ns'], event['finished_ns']
            if (type(started) is not int or type(finished) is not int or
                    not 0 <= started <= finished or type(event['failed']) is not bool):
                raise ValueError('invalid cost event')
            if sequence < warmup:
                continue
            stage = event['stage']
            durations[stage] = durations.get(stage, 0) + finished - started
            calls[stage] = calls.get(stage, 0) + 1
            failures[stage] = failures.get(stage, 0) + event['failed']
        if 'descriptor_fill_total' in durations:
            remaining = durations['descriptor_fill_total'] - durations.get('descriptor_resize', 0)
            if remaining < 0:
                raise ValueError('resize exceeds enclosing descriptor fill')
            durations['descriptor_fill_excluding_resize'] = remaining
        if 'tensor_export_total' in durations:
            remaining = _export_remainder(record['events'])
            if remaining is None:
                exclusive_export_unavailable += 1
            else:
                durations['tensor_export_remaining'] = remaining
        for stage, duration in durations.items():
            totals.setdefault(stage, []).append(duration)
    return dict(measured_samples=sum(sequence >= warmup for sequence in seen),
                exclusive_export_unavailable_samples=exclusive_export_unavailable,
                stages={stage: dict(calls=calls.get(stage), failures=failures.get(stage),
                                    per_sample_ns=_distribution(values))
                        for stage, values in totals.items()},
                scope='diagnostic wall-clock calls; descriptor fill total includes resize; '
                      'exclusive fill retains traversal/validation and instrumentation overhead; '
                      'tensor export total includes decode/map; remaining export requires '
                      'contained nonoverlapping decode/map intervals and still includes instrumentation')
