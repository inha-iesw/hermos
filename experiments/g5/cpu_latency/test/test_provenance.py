"""Real payload mappings must not be mistaken for loaded executable artifacts."""

import pytest

from experiments.g5.cpu_latency import provenance


@pytest.mark.parametrize('legacy_name', ['agnocast', 'herm0s'])
def test_legacy_library_remains_visible_and_fails_new_prefix_validation(tmp_path, monkeypatch, legacy_name):
    legacy = f'/old/install/{legacy_name}lib/lib/lib{legacy_name}.so'
    monkeypatch.setattr(provenance.Path, 'read_text', lambda self:
                        f'1000-2000 r-xp 00000000 00:01 1 {legacy}\n'
                        '2000-3000 r--s 00000000 00:02 2 /dev/shm/hermos@123\n')
    assert provenance.mapped_objects() == [legacy]
    with pytest.raises(RuntimeError, match='non-production'):
        provenance.verify_peer({'loaded_objects': provenance.mapped_objects()},
                               False, prefixes=(tmp_path,))


def test_production_libraries_allow_the_separate_shared_payload_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, 'PREFIX', tmp_path)
    paths = [str(tmp_path / name) for name in (
        '_native.cpython-test.so', '_types.cpython-test.so',
        'libhermos.so', 'libhermos_heaphook.so')]
    provenance.verify_peer({'loaded_objects': paths + ['/dev/shm/hermos@123']}, True)


def test_debug_library_outside_production_prefix_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, 'PREFIX', tmp_path)
    with pytest.raises(RuntimeError, match='non-production'):
        provenance.verify_peer({'loaded_objects': ['/unrelated/libhermos.so']}, True)


def test_dds_peer_rejects_heaphook_even_inside_production_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, 'PREFIX', tmp_path)
    with pytest.raises(RuntimeError, match='DDS.*heaphook'):
        provenance.verify_peer(
            {'loaded_objects': [str(tmp_path / 'libhermos_heaphook.so')]}, False)


@pytest.mark.parametrize('missing', ['_types.cpython-test.so', 'libhermos_heaphook.so'])
def test_hermos_peer_requires_complete_project_library_inventory(tmp_path, monkeypatch, missing):
    monkeypatch.setattr(provenance, 'PREFIX', tmp_path)
    paths = [str(tmp_path / name) for name in (
        '_native.cpython-test.so', '_types.cpython-test.so',
        'libhermos.so', 'libhermos_heaphook.so') if name != missing]
    with pytest.raises(RuntimeError, match='inventory is incomplete'):
        provenance.verify_peer({'loaded_objects': paths}, True)


def test_source_archive_inputs_cover_c_headers_templates_and_cargo_config(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, 'ROOT', tmp_path)
    wanted = ['src/trace.c', 'src/dlpack.h', 'src/version.hpp.in', 'src/.cargo/config.toml']
    for name in wanted + ['src/target/old.c', 'src/__pycache__/old.py']:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('content\n')
    assert set(provenance.hash_sources(['src'])) == set(wanted)
