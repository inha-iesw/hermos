"""Re-evaluate preserved G5 runs without modifying their evidence or verdicts."""

import argparse
import hashlib
import json
from pathlib import Path

from experiments.g5.cpu_relay.analysis import analyze_chain
from experiments.g5.harness import verify_teardown
from .analysis import compare, evaluate, resource_window


def load_run(path):
    """Require per-case raw timestamps and teardown, preserving input hashes."""
    hashes = {}

    def read(name):
        raw = (path / name).read_bytes()
        hashes[str(name)] = hashlib.sha256(raw).hexdigest()
        return json.loads(raw)

    manifest = read('protocol.json')
    protocol = manifest['protocol']
    results = []
    names = set()
    for case in manifest['cases']:
        if case['name'] in names:
            raise ValueError('duplicate case in manifest')
        names.add(case['name'])
        directory = Path(case['name'])
        if directory.name != str(directory) or directory.name in ('.', '..'):
            raise ValueError('case name must be a directory basename')
        relay = 'input' in case
        allowed = ('dds', 'hermos', 'herm0s', 'agnocast')
        if relay:
            if (case['input'] not in allowed or case['output'] not in allowed or
                    case['path'] != f"{case['input']}-to-{case['output']}"):
                raise ValueError('invalid relay transport')
        elif case.get('descriptor_transport', case.get('transport')) not in allowed:
            raise ValueError('invalid transport')
        publisher = read(directory / ('source.json' if relay else 'publisher.json'))
        subscriber = read(directory / ('sink.json' if relay else 'subscriber.json'))
        for record, side in ((publisher, 'input'), (subscriber, 'output')):
            expected = dict(case, transport=case[side]) if relay else case
            if record.get('config', record.get('case')) != expected:
                raise ValueError('peer case identity differs from manifest')
        namespace = publisher.get('time_namespace')
        if not isinstance(namespace, str) or not namespace or namespace != subscriber.get('time_namespace'):
            raise ValueError('peer clock namespace mismatch')
        teardown = read(directory / 'teardown.json')
        verify_teardown(teardown, 3 if relay else 2)
        result = evaluate(publisher, subscriber, warmup=protocol['warmup'],
                          samples=protocol['samples'], period_ns=protocol['period_ns'])
        result['timing_diagnostics'] = bool(protocol.get('timing_diagnostics', False))
        # Detect the initial P0 smoke too: it predates the explicit diagnostic flag.
        result['resource_diagnostics'] = bool(protocol.get('resource_diagnostics') or any(
            record.get('resources_after_warmup', {}).get('memory') is not None
            for record in (publisher, subscriber)))
        if relay:
            middle = read(directory / 'relay.json')
            if middle.get('case') != case:
                raise ValueError('relay case identity differs from manifest')
            if middle.get('time_namespace') != namespace:
                raise ValueError('relay clock namespace mismatch')
            chain = analyze_chain(publisher, middle, subscriber,
                                  warmup=protocol['warmup'], samples=protocol['samples'])
            result['complete'] = chain['complete']
            result['missing_relay_records'] = chain['missing_relay_records']
            result['relay_resources'] = resource_window(middle, warmup=protocol['warmup'])
        result['case'] = case
        results.append(result)
    comparisons = []
    controls = {}
    for result in results:
        case = result['case']
        transport = case.get('descriptor_transport', case.get('transport', case.get('path')))
        key = (case.get('repeat', 1), case.get('layout'), case['size'], case['rmw'])
        if transport in ('dds', 'dds-to-dds'):
            if key in controls:
                raise ValueError('duplicate baseline within a repeat')
            controls[key] = result
    for result in results:
        case = result['case']
        transport = case.get('descriptor_transport', case.get('transport', case.get('path')))
        if transport in ('dds', 'dds-to-dds'):
            continue
        key = (case.get('repeat', 1), case.get('layout'), case['size'], case['rmw'])
        baseline = controls.get(key)
        effect = compare(result, baseline) if baseline else dict(valid=False, reason='missing baseline')
        comparisons.append(dict(case=case['name'], baseline=baseline['case']['name'] if baseline else None,
                                **effect))
    return dict(input=str(path.resolve()), protocol=protocol, input_sha256=hashes,
                results=results, comparisons=comparisons)


def report(runs):
    lines = ['# G5 independent evaluation', '',
             'Recomputed from preserved raw timestamps; each protocol/repeat remains separate.',
             'Completion = fill/borrow start to consumer completion (GPU checksum completion '
             'for GPU cases). A period budget is diagnostic, not an application deadline.',
             'Loss invalidates paired latency comparisons. These paced runs do not establish '
             'saturation throughput, leak freedom, independent-run confidence intervals or '
             'the real-workload 10% requirement.', '']
    for run in runs:
        lines += [f"## {run['input']}", '',
                  ('Cost instrumentation enabled: diagnostic timing only.'
                   if any(r.get('timing_diagnostics') for r in run['results']) else
                   'No cost instrumentation.'), '',
                  ('Detailed boundary observations: diagnostic timing only.'
                   if any(r['resource_diagnostics'] for r in run['results']) else
                   'No detailed memory/thread collection at measurement boundaries.'), '',
                  '| Case | Received | Lost | Longest loss burst | Completion mean / p99 µs | '
                  'Received over period | Lost or over period | Max callback gap ms |',
                  '|---|---:|---:|---:|---:|---:|---:|---:|']
        for result in run['results']:
            timing, gap = result['completion_ns'], result['callback_gap_ns']
            latency = f"{timing['mean']/1000:.2f} / {timing['p99']/1000:.2f}" if timing else 'N/A'
            gap_text = f"{gap['max']/1e6:.3f}" if gap else 'N/A'
            lines.append(f"| {result['case']['name']} | {result['received']}/{result['expected']} | "
                         f"{result['lost']} | {result['longest_loss_burst']} | {latency} | "
                         f"{result['consumer_period_exceedances']} | "
                         f"{result['period_budget_unsatisfied']} | {gap_text} |")
        lines += ['', 'Paired completion effects; positive = additional latency:', '']
        for item in run['comparisons']:
            effect = (f"mean {item['mean_change_percent']:+.2f}%, p99 {item['p99_change_percent']:+.2f}%"
                      if item['valid'] else f"INVALID ({item['reason']})")
            lines.append(f"- {item['case']}: {effect}")
        lines += ['', 'Publisher process costs (not internal function timings):', '',
                  '| Case | Window | Observed rows | CPU ms/row | Voluntary switches/row | '
                  'Minor faults/row |', '|---|---|---:|---:|---:|---:|']
        for result in run['results']:
            resource = result['publisher_resources']
            if not resource['available']:
                lines.append(f"| {result['case']['name']} | unavailable | — | — | — | — |")
                continue
            values = []
            for key, scale in (('cpu_seconds', 1000), ('voluntary_context_switches', 1),
                               ('minor_page_faults', 1)):
                value = resource[f'{key}_per_observed_row']
                values.append(f'{value*scale:.3f}' if value is not None else 'unavailable')
            lines.append(f"| {result['case']['name']} | {resource['scope']} | "
                         f"{resource['observed_rows']} | " + ' | '.join(values) + ' |')
        lines += ['', 'Raw JSON also retains sender cadence/drift, fill and consumer distributions, '
                  'receipt reordering, per-peer resource windows and input SHA-256 hashes.', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, action='append', required=True,
                        help='existing results directory; repeat to keep multiple runs separate')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    runs = [load_run(path) for path in args.input]
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'evaluation.json').write_text(json.dumps(runs, indent=2) + '\n')
    (args.output / 'report.md').write_text(report(runs))


if __name__ == '__main__':
    main()
