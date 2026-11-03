"""Audit exported HTML fanout evidence without executing its JavaScript or runners."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import statistics

from experiments.g5.cpu_latency.analysis import analyze, _distribution
from experiments.g5.harness import verify_teardown


def decode_html(text):
    """Decode the exported const DATA JSON literal; never evaluate script text."""
    match = re.search(r'\bconst\s+DATA\s*=\s*', text)
    if match is None:
        raise ValueError('HTML has no const DATA JSON literal')
    value, _ = json.JSONDecoder().raw_decode(text[match.end():])
    if not isinstance(value, dict) or not isinstance(value.get('fanout'), list):
        raise ValueError('HTML DATA must contain a fanout list')
    return value


def audit_case(publisher, subscribers, *, warmup, samples, period_ns):
    """Expose delivery fairness, all-reader completion and additive stage means.

    Stage medians are medians across subscriber-specific means, matching the
    external breakdown's aggregation. They need not add to the median E2E mean.
    Equal-subscriber-weighted means do add, using each subscriber's own receipts.
    Loss remains explicit; the offered-period budget is diagnostic, not an SLA.
    """
    if not subscribers or type(period_ns) is not int or period_ns <= 0:
        raise ValueError('subscribers and a positive integer period_ns are required')
    namespace = publisher.get('time_namespace')
    if not isinstance(namespace, str) or not namespace or any(
            subscriber.get('time_namespace') != namespace for subscriber in subscribers):
        raise ValueError('peer clock namespace mismatch')
    sent = {row[0]: row[1:] for row in publisher['samples']}
    per_subscriber, received_maps = [], []
    stage_names = ('fill', 'in_publish', 'after_publish', 'consume')
    for subscriber in subscribers:
        result = analyze(publisher, subscriber, warmup=warmup, samples=samples)
        received = {row[0]: row[1:] for row in subscriber['samples'] if row[0] >= warmup}
        received_maps.append(received)
        stages = {name: [] for name in stage_names}
        for sequence, (callback, done) in received.items():
            fill, publish, returned = sent[sequence]
            for name, value in zip(stage_names, (publish - fill, min(callback, returned) - publish,
                                                max(callback - returned, 0), done - callback)):
                stages[name].append(value)
        result['stages_ns'] = {name: _distribution(values) for name, values in stages.items()}
        per_subscriber.append(result)
    common = sorted(set.intersection(*(set(received) for received in received_maps)))
    barrier = [max(received[sequence][1] for received in received_maps) - sent[sequence][0]
               for sequence in common]
    received_counts = [result['received'] for result in per_subscriber]
    expected_receipts = samples * len(subscribers)
    complete_stats = all(result['fill_to_consumer_ns'] is not None for result in per_subscriber)
    means = [result['fill_to_consumer_ns']['mean'] for result in per_subscriber] if complete_stats else None
    stage_means = ({name: [result['stages_ns'][name]['mean'] for result in per_subscriber]
                    for name in stage_names} if complete_stats else None)
    stage_medians = ({name: statistics.median(values) for name, values in stage_means.items()}
                     if stage_means else None)
    within = sum(value <= period_ns for value in barrier) if barrier else None
    return dict(
        expected_per_subscriber=samples, received=received_counts,
        aggregate_lost_fraction=(expected_receipts - sum(received_counts)) / expected_receipts,
        worst_subscriber_lost_fraction=max(samples - count for count in received_counts) / samples,
        complete=all(result['complete'] for result in per_subscriber),
        per_subscriber=per_subscriber, all_subscribers_received_ids=common,
        all_subscribers_completion_ns=_distribution(barrier),
        all_subscribers_within_period=within,
        all_subscribers_period_unsatisfied=samples - (within or 0),
        median_subscriber_p50_ns=statistics.median(
            result['fill_to_consumer_ns']['p50'] for result in per_subscriber) if complete_stats else None,
        stage_medians_ns=stage_medians,
        sum_of_stage_medians_ns=sum(stage_medians.values()) if stage_medians else None,
        median_subscriber_e2e_means_ns=statistics.median(means) if means else None,
        equal_subscriber_mean_e2e_ns=statistics.fmean(means) if means else None,
        equal_subscriber_mean_stages_ns={name: statistics.fmean(values)
                                        for name, values in stage_means.items()} if stage_means else None)


def load_fanout(path):
    """Validate each declared case from raw peers and teardown; retain read hashes."""
    hashes = {}

    def read(relative):
        source = path / relative
        raw = source.read_bytes()
        hashes[str(source.resolve())] = hashlib.sha256(raw).hexdigest()
        return json.loads(raw)

    manifest = read('protocol.json')
    protocol = manifest['protocol']
    results, names = [], set()
    for case in manifest['cases']:
        name, count = case['name'], case['subscribers']
        if name in names or Path(name).name != name or name in ('.', '..'):
            raise ValueError('duplicate or invalid case directory name')
        if type(count) is not int or count < 1:
            raise ValueError('subscribers must be a positive integer')
        names.add(name)
        directory = Path(name)
        publisher = read(directory / 'publisher.json')
        subscribers = [read(directory / f'subscriber-{index}.json')
                       for index in range(1, count + 1)]
        verify_teardown(read(directory / 'teardown.json'), count + 1)
        result = audit_case(publisher, subscribers, warmup=protocol['warmup'],
                            samples=protocol['samples'], period_ns=protocol['period_ns'])
        results.append(dict(case=case, **result))
    counts = {}
    for result in results:
        variant = result['case']['variant']
        group = ('reclaim_off' if variant.endswith('-noreclaim') else
                 'default_hermos' if 'hermos' in variant else
                 'vanilla' if 'vanilla' in variant else 'other')
        for category in ('total', group):
            value = counts.setdefault(category, dict(cases=0, complete_cases=0,
                                                     expected_receipts=0, received=0,
                                                     all_subscribers_received=0))
            value['cases'] += 1
            value['complete_cases'] += result['complete']
            value['expected_receipts'] += result['expected_per_subscriber'] * len(result['received'])
            value['received'] += sum(result['received'])
            value['all_subscribers_received'] += len(result['all_subscribers_received_ids'])
    for value in counts.values():
        value['lost'] = value['expected_receipts'] - value['received']
    return dict(input=str(path.resolve()), protocol=protocol, input_sha256=hashes,
                counts=counts, variant_case_counts=dict(Counter(
                    result['case']['variant'] for result in results)), results=results,
                period_budget_scope='diagnostic offered-period comparison, not a deadline SLA',
                stage_aggregation='median and equal-weighted mean of subscriber-specific stage means')


def compare_html(rows, results):
    """Compare displayed p50 to its original median-of-subscriber-p50 definition.

    HTML milliseconds have two decimal places, so allow half a displayed unit.
    This numerical match does not validate the metric as all-reader completion.
    """
    index = {}
    for row in rows:
        key = (row['scenario'], row['variant'], row['size'], row['n'])
        if key in index:
            raise ValueError('duplicate HTML fanout condition')
        index[key] = row
    comparisons = []
    used = set()
    for result in results:
        case = result['case']
        key = (case['scenario'], case['variant'], case['size'], case['subscribers'])
        row = index.get(key)
        used.add(key)
        computed = result['median_subscriber_p50_ns']
        actual = computed / 1e6 if computed is not None else None
        shown = row.get('e2e_p50_ms') if row is not None else None
        p50_match = row is not None and (
            shown is None and actual is None or
            type(shown) in (int, float) and actual is not None and abs(shown - actual) <= 0.005000001)
        received_match = row is not None and row.get('received') == result['received']
        comparisons.append(dict(condition=list(key), html_present=row is not None,
                                received_match=received_match, p50_match=p50_match,
                                html_p50_ms=shown, recomputed_p50_ms=actual,
                                matches=received_match and p50_match))
    return dict(matches=sum(item['matches'] for item in comparisons), cases=comparisons,
                unmatched_html_conditions=[list(key) for key in index if key not in used],
                p50_rounding_tolerance_ms=0.005000001)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--html', required=True, type=Path)
    parser.add_argument('--fanout-results', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError(args.output)
    raw = args.html.read_bytes()
    data = decode_html(raw.decode('utf-8'))
    audit = load_fanout(args.fanout_results)
    audit['input_sha256'][str(args.html.resolve())] = hashlib.sha256(raw).hexdigest()
    audit['html_comparison'] = compare_html(data['fanout'], audit['results'])
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'html-data.json').write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
    (args.output / 'audit.json').write_text(json.dumps(audit, indent=2, ensure_ascii=False) + '\n')


if __name__ == '__main__':
    main()
