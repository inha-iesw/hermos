"""Fail closed when the trace lacks coverage or contains a payload copy."""

import json
import sqlite3

import pytest

from analyze import analyze


def trace(tmp_path, *, kind=2, size=8, wrong_pid=False, missing_range=False,
          missing_import=False, combined_api_table=False):
    database = tmp_path / 'trace.sqlite'
    connection = sqlite3.connect(database)
    connection.executescript('''
        CREATE TABLE StringIds(id INTEGER, value TEXT);
        CREATE TABLE NVTX_EVENTS(start INTEGER, end INTEGER, globalTid INTEGER,
                                 textId INTEGER, text TEXT);
        CREATE TABLE CUPTI_ACTIVITY_KIND_DRIVER(start INTEGER, end INTEGER,
            globalTid INTEGER, correlationId INTEGER, nameId INTEGER, returnValue INTEGER);
        CREATE TABLE CUPTI_ACTIVITY_KIND_RUNTIME(start INTEGER, end INTEGER,
            globalTid INTEGER, correlationId INTEGER, nameId INTEGER, returnValue INTEGER);
        CREATE TABLE CUPTI_ACTIVITY_KIND_MEMCPY(globalPid INTEGER,
            correlationId INTEGER, bytes INTEGER, copyKind INTEGER);
        CREATE TABLE ENUM_CUDA_MEMCPY_OPER(id INTEGER, label TEXT);
        INSERT INTO StringIds VALUES (1, 'cuMemImportFromShareableHandle'),
            (2, 'cudaMemcpyAsync');
        INSERT INTO ENUM_CUDA_MEMCPY_OPER VALUES (1, 'HtoD'), (2, 'DtoH'), (8, 'DtoD');
    ''')
    for index in range(8):
        start = index * 100
        for pid, name in ((101, 'g4_publish'), (102, 'g4_consume')):
            if not (missing_range and index == 0 and pid == 101):
                connection.execute('INSERT INTO NVTX_EVENTS VALUES (?, ?, ?, NULL, ?)',
                                   (start, start + 90, (pid << 24) | 5, name))
        connection.execute('INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME VALUES (?, ?, ?, ?, 2, 0)',
                           (start + 20, start + 30, (102 << 24) | 5, index))
        connection.execute('INSERT INTO CUPTI_ACTIVITY_KIND_MEMCPY VALUES (?, ?, ?, ?)',
                           ((103 if wrong_pid else 102) << 24, index, size, kind))
    if not missing_import:
        connection.execute('INSERT INTO CUPTI_ACTIVITY_KIND_DRIVER VALUES (?, ?, ?, 99, 1, 0)',
                           (10, 15, (102 << 24) | 5))
    if combined_api_table:
        connection.executescript('''
            INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME SELECT * FROM CUPTI_ACTIVITY_KIND_DRIVER;
            DROP TABLE CUPTI_ACTIVITY_KIND_DRIVER;
        ''')
    connection.commit()
    connection.close()
    pair = tmp_path / 'pair.json'
    pair.write_text(json.dumps(dict(data_pass=True, returncodes=[0, 0], pids=[101, 102],
                                    payload_bytes=64)))
    return database, pair


@pytest.mark.parametrize('combined_api_table', [False, True])
def test_scalar_readback_with_vmm_import_passes(tmp_path, combined_api_table):
    result = analyze(*trace(tmp_path, combined_api_table=combined_api_table))
    assert result['pass']
    assert len(result['marked_copies']) == 8
    assert result['all_peer_resource_api_invocations']['cuMemImportFromShareableHandle'] == {
        'calls': 1, 'returns': {'0': 1}}


@pytest.mark.parametrize('change', [dict(kind=1), dict(kind=8), dict(size=64),
                                  dict(wrong_pid=True), dict(missing_range=True),
                                  dict(missing_import=True)])
def test_missing_evidence_or_payload_copy_fails(tmp_path, change):
    assert not analyze(*trace(tmp_path, **change))['pass']
