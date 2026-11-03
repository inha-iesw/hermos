"""Per-branch delivery and same-ID completion, with loss-aware paired effects."""

import json

from experiments.g5.evaluation.analysis import evaluate
from experiments.g5.evaluation.external import audit_case


def analyze_fanout(case, publisher, subscribers):
    """Keep receipt gaps and cadence alongside the all-subscriber completion audit."""
    settings = {key: case[key] for key in ('warmup', 'samples', 'period_ns')}
    result = audit_case(publisher, subscribers, **settings)
    per_subscriber = [evaluate(publisher, subscriber, **settings) for subscriber in subscribers]
    for existing, evaluated in zip(result['per_subscriber'], per_subscriber):
        existing.update({key: evaluated[key] for key in (
            'missing_sequences', 'longest_loss_burst', 'reordered_receipts',
            'callback_gap_ns', 'consumer_period_exceedances', 'subscriber_resources')})
    sender = per_subscriber[0]
    result.update({key: sender[key] for key in (
        'sender_gap_ns', 'sender_drift_ns', 'observed_send_hz', 'publisher_resources')})
    result.update(case=case, lost=sum(item['lost'] for item in per_subscriber),
                  expected_receipts=case['samples'] * len(subscribers),
                  latency_scope='conditional on received IDs; loss invalidates paired acceptance',
                  p99_scope=f"exploratory: {case['samples']} scheduled samples; actual receipts may be fewer")
    return result


def pair_key(case):
    return (case['size'], case['subscribers'], case['rate_hz'], case['rmw'], case['repeat'])


def paired_effects(results):
    """Compare the same load/repeat, never pool samples or select complete readers."""
    controls = {pair_key(result['case']): result for result in results
                if result['case']['transport'] == 'vanilla'}
    effects = []
    for result in results:
        if result['case']['transport'] != 'hermos':
            continue
        control = controls.get(pair_key(result['case']))
        effect = dict(case=result['case']['name'], baseline=control['case']['name'] if control else None,
                      valid=False)
        if control is None:
            effect['reason'] = 'missing baseline'
        elif not result['complete'] or not control['complete']:
            effect['reason'] = 'receipt loss; survivor latency is not paired acceptance'
        else:
            current = result['all_subscribers_completion_ns']
            reference = control['all_subscribers_completion_ns']
            if not current or not reference or any(reference[stat] <= 0 for stat in ('mean', 'p50', 'p99')):
                effect['reason'] = 'unavailable or zero baseline completion'
            else:
                effect.update(valid=True, **{stat + '_change_percent':
                              100 * (current[stat] / reference[stat] - 1)
                              for stat in ('mean', 'p50', 'p99')})
        effects.append(effect)
    return effects


def report(results, output, protocol):
    """Persist every condition, including overload/loss and sparse-tail limitations."""
    comparisons = paired_effects(results)
    (output / 'comparisons.json').write_text(json.dumps(comparisons, indent=2) + '\n')
    lines = ['# GPU Python fanout: host staging versus CUDA IPC', '',
             'One publisher and N separate subscriber processes; full uint8 GPU checksum.',
             'Both publishers use a ROS timer. Rates label offered load, not achieved throughput.',
             '5 Hz is a no-loss candidate, 30 Hz a higher-load candidate; receipts decide the outcome.',
             f"P99 is exploratory with {protocol['samples']} scheduled samples; actual receipts may be fewer.",
             'All-subscriber latency is conditional on common received IDs. Missing IDs remain failures.',
             'The offered period is a diagnostic budget, not an application deadline.', '',
             '| Case | Received by each subscriber | Aggregate loss % | Worst loss % | Common IDs | All-reader mean / p99 ms | IDs within period | Sender Hz |',
             '|---|---|---:|---:|---:|---:|---:|---:|']
    for result in results:
        timing = result['all_subscribers_completion_ns']
        latency = f"{timing['mean']/1e6:.3f} / {timing['p99']/1e6:.3f}" if timing else 'unavailable'
        rate = f"{result['observed_send_hz']:.3f}" if result['observed_send_hz'] is not None else 'unavailable'
        lines.append(f"| {result['case']['name']} | {result['received']} | "
                     f"{100*result['aggregate_lost_fraction']:.2f} | "
                     f"{100*result['worst_subscriber_lost_fraction']:.2f} | "
                     f"{len(result['all_subscribers_received_ids'])}/{result['case']['samples']} | "
                     f"{latency} | {result['all_subscribers_within_period'] or 0} | "
                     f"{rate} |")
    lines += ['', 'Paired all-reader completion change (positive means additional latency):', '']
    for effect in comparisons:
        value = (f"mean {effect['mean_change_percent']:+.2f}%, p50 {effect['p50_change_percent']:+.2f}%, "
                 f"p99 {effect['p99_change_percent']:+.2f}%" if effect['valid'] else
                 f"INVALID ({effect['reason']})")
        lines.append(f"- {effect['case']}: {value}")
    lines += ['', 'This host-staging comparison changes the payload path. It is separate from the',
              'G5 same-CUDA-core DDS/HERM0S descriptor comparison. No reclaim-policy variant is used.',
              'Repeated process runs remain separate; no message-level confidence interval is claimed.', '']
    (output / 'report.md').write_text('\n'.join(lines))
