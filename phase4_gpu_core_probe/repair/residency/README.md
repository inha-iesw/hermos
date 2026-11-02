# CUDA IPC residency diagnostics

These standalone probes separate CUDA shared-memory residue from HERMOS and
the repaired core. They neither unlink `cuda.shm.*` nor reset a CUDA device.
Run without concurrent GPU workloads so before/after changes can be attributed.
All modes use device 0; this is a diagnostic, not an adoption PASS test.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 \
  phase4_gpu_core_probe/repair/residency/event_probe.py \
  --stage /absolute/new-event-residency
```

`event_probe.py` starts fresh subprocesses for driver event create/export,
normal/abrupt importer exit, runtime allocation/callback, runtime IPC event and
mixed runtime/driver event operations. The exporting event remains alive until
the importer exits. Every explicit CUDA call is checked. `result.json` contains
per-process call results and per-cycle created/removed names and sizes. Exit zero
means the diagnostic completed; it does **not** mean that no objects remained.

On the tested Thor/CUDA 13.2/driver 595.78 combination, each of two runtime-only
IPC-event subprocesses left one 81,920-byte `cuda.shm.*` object after successful
event destroy and process exit. Two mixed runtime/driver subprocesses did the
same. The other eight cycles added no objects: 1,209 before, 1,213 after. Evidence:
`log/g3/completion/runtime-event-residency/result.json`. This reproducer uses no
HERMOS, heaphook or repaired core; it does not establish physical VRAM leakage
or the driver's internal ownership rules.

`vmm_probe.cpp` checks driver-only VMM allocate/map/unmap/release and exported-FD
ownership, with fresh normal/abrupt importer subprocesses. It is built as a
diagnostic target by the existing repaired probe CMake configuration; it is not
registered as a CTest adoption check.

```bash
cmake --build build/hermos/repair-probe/phase4_gpu_core_probe \
  --target g3_vmm_residency --parallel 2
build/hermos/repair-probe/phase4_gpu_core_probe/repair/g3_vmm_residency allocate
build/hermos/repair-probe/phase4_gpu_core_probe/repair/g3_vmm_residency export
build/hermos/repair-probe/phase4_gpu_core_probe/repair/g3_vmm_residency pair
build/hermos/repair-probe/phase4_gpu_core_probe/repair/g3_vmm_residency crash_pair
```

The recorded VMM cycles added no `cuda.shm.*` objects; see
`log/g3/completion/vmm-residency/result.json` and
`log/g3/completion/vmm-ipc-residency/result.json`. VMM output lists CUDA return
codes; shared-memory inventories in those records were collected externally.
These narrower controls do not cancel the runtime IPC-event result or prove a
physical GPU memory plateau. G3's supported execution boundary now uses a fresh
private IPC/mount namespace per epoch through the [recovery supervisor](../recovery/README.md).
The residue remains private until every peer, adopted daemon and the worker exit;
the operating contract does not manually delete driver objects or claim to repair
the CUDA runtime. The unisolated commands above still reproduce residue on the
tested driver. See EVIDENCE §35 for repeated isolated execution and host inventory
preservation; the original no-unbounded-accumulation condition is unchanged.
