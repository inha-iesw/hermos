"""Correlate marked API calls to GPU copies using the nsys SQLite schema."""

from collections import Counter
import json
from pathlib import Path
import sqlite3
import sys


def analyze(database, pair_path):
    pair = json.loads(Path(pair_path).read_text())
    connection = sqlite3.connect(Path(database).as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    with connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        ranges = list(connection.execute('''
            SELECT n.start, n.end, n.globalTid, coalesce(s.value, n.text) AS name
            FROM NVTX_EVENTS n LEFT JOIN StringIds s ON n.textId=s.id
            WHERE coalesce(s.value, n.text) IN ('g4_publish', 'g4_consume')
        '''))
        # nsys encodes process identity in the upper globalTid bits. Correlation
        # IDs alone are not unique between the two independently traced peers.
        # nsys 2025.6 exports both driver and runtime calls in RUNTIME, using
        # eventClass to distinguish them. Older exports have a DRIVER table.
        api_tables = ['CUPTI_ACTIVITY_KIND_RUNTIME']
        if 'CUPTI_ACTIVITY_KIND_DRIVER' in tables:
            api_tables.append('CUPTI_ACTIVITY_KIND_DRIVER')
        calls = []
        for table in api_tables:
            calls.extend(dict(row) for row in connection.execute(f'''
                SELECT a.start, a.end, a.globalTid, a.correlationId,
                       a.returnValue, s.value AS name
                FROM {table} a JOIN StringIds s ON a.nameId=s.id
            '''))
        copies = list(connection.execute('''
            SELECT m.rowid AS id, m.globalPid, m.correlationId, m.bytes,
                   m.copyKind, e.label AS kind
            FROM CUPTI_ACTIVITY_KIND_MEMCPY m
            JOIN ENUM_CUDA_MEMCPY_OPER e ON m.copyKind=e.id
        '''))
        diagnostics = []
        if 'DIAGNOSTIC_EVENT' in tables:
            diagnostics = [dict(row) for row in connection.execute('''
                SELECT (globalPid >> 24) & 16777215 AS pid, severity, text
                FROM DIAGNOSTIC_EVENT WHERE severity >= 2
            ''')]
    connection.close()
    counts = Counter(row['name'] for row in ranges)
    marked = [call for call in calls if any(
        call['globalTid'] == span['globalTid'] and span['end'] is not None
        and span['start'] <= call['start'] and call['end'] <= span['end']
        for span in ranges)]
    keys = {(call['globalTid'] & 0xFFFFFFFFFF000000, call['correlationId'])
            for call in marked}
    selected = [dict(row) for row in copies
                if (row['globalPid'], row['correlationId']) in keys]
    api_counts = Counter(call['name'] for call in marked)
    vmm = {name: count for name, count in api_counts.items()
           if name.startswith(('cuMemImportFromShareableHandle', 'cuMemMap', 'cuMemSetAccess'))}
    range_pids = sorted({(span['globalTid'] >> 24) & 0xFFFFFF for span in ranges})
    peer_calls = [call for call in calls
                  if (call['globalTid'] >> 24) & 0xFFFFFF in pair['pids']]
    resource_calls = [call for call in peer_calls
                      if call['name'].startswith(('cuMem', 'cuEvent', 'cuIpc', 'cuStreamWaitEvent'))]
    resource_counts = {}
    for call in resource_calls:
        entry = resource_counts.setdefault(call['name'], dict(calls=0, returns={}))
        entry['calls'] += 1
        value = str(call['returnValue'])
        entry['returns'][value] = entry['returns'].get(value, 0) + 1
    # CUPTI copyKind 2 is device-to-host. Only the explicit uint8 sum().item()
    # result (int64) may leave the GPU; no payload-size or other copies pass.
    valid_copies = len(selected) == 8 and all(
        row['copyKind'] == 2 and row['bytes'] == 8 for row in selected)
    report = dict(
        pass_=bool(pair.get('data_pass') and pair['returncodes'] == [0, 0]
                   and counts == {'g4_publish': 8, 'g4_consume': 8}
                   and range_pids == sorted(pair['pids']) and valid_copies
                   and any(name.startswith('cuMemImportFromShareableHandle') for name in vmm)),
        diagnostic_only=True, range_counts=dict(counts), traced_pids=range_pids,
        payload_bytes=pair['payload_bytes'], marked_cuda_api_counts=dict(api_counts),
        marked_copies=selected, marked_vmm_api_counts=vmm,
        cuda_api_tables=api_tables, all_peer_resource_api_invocations=resource_counts,
        all_trace_copy_count=len(copies), diagnostics=diagnostics,
        scope='Eight uint8 transfers; marked publish/consume regions. Startup excluded. '
              'Scalar result copies included. Resource API invocation counts include '
              'startup and shutdown and are not live-resource or high-water counters. '
              'Software-instrumented CUDA trace; Unified Memory tracing unavailable on '
              'this device. No physical VRAM or performance claim.')
    report['pass'] = report.pop('pass_')
    return report


if __name__ == '__main__':
    result = analyze(sys.argv[1], sys.argv[2])
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['pass'] else 1)
