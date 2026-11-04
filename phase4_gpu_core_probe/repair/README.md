# Bounded GPU core repair and native HERMOS integration

> Naming migration: these commands use HERMOS. Existing EVIDENCE and device logs
> describe the pre-rename Agnocast binaries. Rebuild all peers/providers and the
> device artifact bundle at `build/hermos/device-arm64` before a new device run;
> do not reuse or relabel the old G2 manifest. See the [migration guide](../../README.md).

Execution results, registered target counts and the scope of each complete or
targeted run are recorded in [EVIDENCE.md](EVIDENCE.md). The latest implementation
includes session tooling, failure recovery and a supervisor that creates a fresh
private IPC/mount namespace and `/dev/shm` for every complete peer-group epoch.
The privilege and lifecycle defects identified after §35 have been corrected
and verified in §36. G3 is accepted for the tested Thor configuration under the
restricted recovery contract. Current target coverage combines a full diagnostic
run with the final recovery-target rerun; it is not one uninterrupted full-suite
execution of the final source. EVIDENCE separates these runs and the failed trials.
CUDA runtime IPC-event residue remains reproducible inside each private epoch;
complete worker/descendant teardown contains it without deleting driver names.
The [recovery README](recovery/README.md) is the authoritative operating contract.
Python GPU Tensor ownership (G4),
Orin validation and performance/copy profiling (G5) remain unfinished.
The original rejection diagnostics and upstream baseline are preserved in
[the parent README](../README.md).

## Source and compatibility

`prepare.py` reconstructs the locked upstream commit
`0e8933c8e58ffa7f1240a4c9efda3a2b84e5398c` with GNU patch, zero fuzz, and verifies
every resulting source file. It neither modifies the pristine tree nor invokes
Git. The MIT core patch is `patches/core-lifecycle.patch`, SHA256
`e500ce18ffead8ad657739a3ec090ea2ca202aef540d4ec568acac52f88ac5f4`
(23 patched core files, 140 reconstructed sources). Two core files changed from
the §34–35 baseline; the corrected revision and verification are recorded in
EVIDENCE §36.

The existing CUDA VMM allocation/import, SCM_RIGHTS FD transfer and IPC event
engine remain in use. C++ API changes require rebuilding native consumers:
`acquire_for_publish(CUstream)` binds the producer stream, `BufferMapper(Config)`
requires limits, and producer `try_close()` reports a busy/failed pool. Mapper `try_close()` seals
the shared read epoch and polls full read cleanup; `closing()` observes that seal
on every alias, including after successful drain. `CudaDeviceContext::cleanup_failed()`
replaces the earlier primary-release-only query and includes context restoration failures. Manager, pool and
mapper `poisoned()` queries report terminal failure. An unpoisoned mapper may still
be sealed; callers check `closing()` separately. False is not a promise that the
next operation will succeed. The metadata layout
remains 24 bytes, but the writer protocol changes; its private namespace is
`/rclpy_zc_gpu_v1_<pid>_<block>`. Legacy and repaired peers must not be mixed.
The original Python bindings/examples have not been ported to these APIs, and
their earlier 73-case baseline is not a repaired-source test result.
Upstream `BufferCore.msg` comments and `doc/buffer_metadata_protocol.md` describe
the legacy protocol; this patch and the contract below govern repaired peers.

`rclpy_hermos_gpu_msgs/msg/GpuTensor.msg` carries the unchanged core descriptor
plus dtype, dense shape/element strides, byte offset and allocation size. Typed
HERMOS loans carry this CPU metadata. GPU production writes directly into the
core allocation; consumption uses its mapped read handle. The probe returns a
four-byte GPU checksum to CPU, without staging the 64-byte payload on CPU.
This is source and functional evidence for this path, not profiler proof.
CPU adapter ABI 3 and the Python CPU API are unchanged.

The native consumer's actual VMM mapping is read/write: the transport probe calls
`cuMemGetAccess` on the mapped address and observes
`CU_MEM_ACCESS_FLAGS_PROT_READWRITE` (3). Consumer read ownership is a lifetime
contract, not hardware-enforced read-only access inherited from the CPU heap.

## Scope of the safety contract

- One live producer pool epoch per process. Outstanding blocks retain their pool.
  Pending producer events prevent reuse, including cancellation. Writer admission
  excludes readers while the UID changes, and stale/unpublished UIDs are rejected.
- Committed blocks retain the upstream 100 ms grace before reuse, even after GPU
  completion and the last reader release. GPU completion and reader release are
  additional requirements; grace alone never permits reuse. Unpublished cancel
  waits for recorded GPU completion without a committed-publication grace.
  `block_count / 100 ms` is an ideal sustained-publication upper bound for one
  healthy pool epoch, not a throughput guarantee or a sufficient pool-size rule.
- Streams must belong to the selected device's primary context and remain valid
  through completion recording. Primary-context legacy/default and explicit streams
  are supported; per-thread default and foreign-context streams are rejected.
  Already-bound owners activate their retained primary context for cleanup on
  another host thread, restoring that thread's previous context afterward.
- All mapper aliases share mandatory `H` read slots and maximum message size `M`.
  A permit precedes import and remains charged through active, deferred and failed
  cleanup. Recreating a mapper cannot bypass a live epoch's limits or its shutdown seal. Strict mapping
  keeps no idle metadata/import cache. Each admitted message imports the VMM
  allocation and ready event again; retry after a real `BufferMapper::map()` bind
  failure repeats that import. This resource-bound policy has an unmeasured cost.
- FD replies must contain exactly one SCM_RIGHTS descriptor without `MSG_CTRUNC`.
  Invalid replies return every installed FD before CUDA context acquisition, including
  descriptors placed in ancillary alignment padding. The accepted single FD
  remains scoped through context acquisition and is closed after the import call.
  Scoped FD owners cannot be copied. A publisher EOF is diagnosed separately
  from a failed `recvmsg`; only the latter reports `errno`. Failed close-on-exec
  setup on exported, listener, request or accepted FDs closes the owned FD and
  stops that operation. Socket cleanup unlinks only a path this server bound.
- Completion polling can release a finished read behind a stalled read. Pool close
  claims idle blocks exclusively and refuses pending writes or retained readers.
  Healthy busy final destruction retains one draining pool without poisoning it.
  A later `initialise()` retries its close before allocating a replacement; callers
  retry while old work/readers remain. There is no background publisher reaper.
  An initialisation failure whose rollback succeeds returns the process epoch,
  owned names and FDs; the same manager or a replacement may retry. Allocation
  exceptions roll back before propagating to the caller. FD-server thread creation
  failure returns false after rollback. The completion worker avoids heap allocation
  while processing and waiting; queue creation/enqueue failure retains the charged
  read and poisons admission instead of releasing a possibly live read.
  Failed mapping/event cleanup retains resources/references; primary-context release failure
  permanently blocks new GPU admission and retains in the process. Context pop
  failure or a mismatched popped context also disables admission. Ambiguous
  cleanup is never retried to make a failed epoch appear healthy.
- A dead consumer's reference is not reclaimed. Its fixed slot remains unavailable.
  A dead reader can therefore prevent the draining pool from ever closing.
  Recovery then requires restarting all involved GPU peers. There is no live-pool
  transparent crash recovery or holder ledger. The inherited global dead-PID
  metadata scavenger has been removed: a PID invisible in this namespace does
  not establish ownership of its IPC names. Existing-name collisions also fail
  initialization without unlinking that object, even when the PID matches.
  Earlier names created by the failed pool are rolled back. This preserves peer
  ownership; it does not establish full cross-PID-namespace deployment support.
  SIGKILL leaves metadata/socket names.
  Tests and the registered-epoch supervisor remove only recorded names after
  every owned GPU peer has exited.

For manager/mapper callers the import mapping bound is at most H mappings of
`align(M, CUDA allocation granularity)` bytes each, including retained failures.
This is a mapping bound, not a physical VRAM bound. Primary-release poisoning
allows only already admitted owners to finish, bounded by H mapper admissions
plus the producer pool. Arbitrary use of low-level core helpers is outside this
experiment's contract.
The installed legacy `BufferMetadataCache`, `IpcHandleCache` and direct VMM importer
are unused by strict mapping and do not enforce its H bound. Subscriber shutdown follows the explicit contract below.

## Shutdown and restart contract

The process GPU runtime owner stops dispatching new reads, then calls mapper
`try_close()` to seal admission across every mapper alias. Individual endpoint
close only releases that endpoint's owners; it must not seal the process epoch.
Unlike producer close, even a busy false mapper result changes admission state.
`closing()` is true for all existing and newly joined aliases, including after
drain. A false result with `closing() && !poisoned()` means active/importing/deferred
reads still exist. Moved-from mappers return false from both close and closing.
Release read owners while their
streams remain valid, let GPU work complete, and retry without resetting CUDA.
Success means context restoration, completion event cleanup, imported-resource
cleanup and reference/permit release are complete. The process worker remains
idle; its queue being empty alone is not a drain condition. A fresh read epoch
requires every old mapper and owner to disappear. Mapper destruction alone does
not seal other aliases.

After subscriber drain, close the producer pool before CUDA/runtime teardown.
On poison or a dead reader, stop all involved GPU peers and restart them together;
a live producer cannot reclaim the stranded reference. Clean only metadata and
socket names recorded by the supervisor for those confirmed-dead peers. Do not
glob-delete names or infer ownership from `kill(pid, 0)` in another PID namespace.
The [owned-peer recovery operating contract](recovery/README.md) owns the CLI/API
rules, installation instructions and supported peer environment. It requires an
administrator-installed root-owned bootstrap executed by system Python with
`-I -B`; caller-writable checkout code is reached only after privilege drop.
Supplementary groups come from the account database, and the bootstrap measures
the shm capacity itself. A sudo rule must name that fixed helper, never generic
Python or a checkout worker.

The current scope is complete groups of directly executed, registered probe
peers, each using `HERMOS_BRIDGE_MODE=off` and
`HERMOS_NO_DISCOVERY_AGENT=1`. `IsolatedEpoch` enforces those effective
environment values. `ros2 run/launch`, unregistered stock/G1/G2 nodes and DDS
connectivity across the private mount are not validated integrations. Outside
HERMOS/GPU connections are forbidden by contract; `/tmp` FD sockets are shared
paths, not an enforced security boundary. All ownership, timeout, parent-death,
no-adoption and restart rules are specified in the recovery README. The
lower-level `Group` API does not provide namespace isolation by itself.
Controlled crash/restart and namespace runs are separate evidence; their exact
revision and execution status are in EVIDENCE.

## Reproduce

Run from the workspace root in a fresh shell. Host Ubuntu **24.04.5** was supplied
by the user; the tested Docker user space is Ubuntu **22.04.5**, ROS Humble,
Python 3.10.12, CUDA 13.2.86/driver 595.78 and Torch 2.13.0+cu132. The GPU is
Thor, capability 11.0, with kernel 6.8.12-1021-tegra/L4T R39.2.1. Architecture 110
below is specific to this observed configuration; it is not an Orin setting.
The existing CPU adapter must already be installed in `install/`. Build order is
repaired core/messages → G4 adapter → G3 probe: the probe consumes the public
descriptor validator installed by `rclpy_hermos_gpu`, and G4 interoperability
tests then consume the probe executable. The core build itself does not need G4.

```bash
mkdir -p log/hermos/g3
export PYTHONDONTWRITEBYTECODE=1
python3 phase4_gpu_core_probe/repair/prepare.py > log/hermos/g3/repair-sources.json
source /opt/ros/humble/setup.bash
CMAKE_BUILD_PARALLEL_LEVEL=2 colcon --log-base log/hermos/g3/repair/core-build build \
  --base-paths build/g3/repaired/ros2_cuda_ipc/ros2_cuda_ipc_msgs \
    build/g3/repaired/ros2_cuda_ipc/ros2_cuda_ipc_core rclpy_hermos_gpu_msgs \
  --build-base build/hermos/gpu-core --install-base install/hermos/gpu-core \
  --packages-select ros2_cuda_ipc_msgs ros2_cuda_ipc_core rclpy_hermos_gpu_msgs \
  --cmake-args -DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=Debug
source install/hermos/verify/local_setup.bash
source install/hermos/gpu-core/local_setup.bash
CMAKE_BUILD_PARALLEL_LEVEL=2 colcon --log-base log/hermos/g4/adapter-build build \
  --packages-select rclpy_hermos_gpu \
  --build-base build/hermos/gpu-test --install-base install/hermos/gpu-test --symlink-install \
  --cmake-args -DBUILD_TESTING=ON -DCMAKE_BUILD_TYPE=Debug
source install/hermos/gpu-test/local_setup.bash
CMAKE_BUILD_PARALLEL_LEVEL=2 colcon --log-base log/hermos/g3/repair/probe-build build \
  --base-paths phase4_gpu_core_probe --build-base build/hermos/repair-probe \
  --install-base install/hermos/repair-probe \
  --cmake-args -DG3_REPAIRED_CORE=ON -DBUILD_TESTING=ON \
    -DCMAKE_BUILD_TYPE=Debug -DCMAKE_CUDA_ARCHITECTURES=110
python3 phase4_gpu_core_probe/repair/session/run_device.py \
  "$PWD/log/hermos/g3/repair-device-session" env G3_RUN_TRANSPORT=1 \
  ctest --test-dir build/hermos/repair-probe/phase4_gpu_core_probe \
  -R '^(test_peer_recovery|test_isolated_epoch|test_namespace_worker|test_namespace_device)$' \
  --output-on-failure --no-tests=error
```

This selects the namespace acceptance suite. Omitting `-R` runs the full diagnostic
suite, including earlier unisolated cases that can leave host `cuda.shm.*` residue
on the tested driver. The operational entry point is `recovery/run.py`; its
command manifest and restart options are documented in the
[recovery guide](recovery/README.md).

The process tests require `/dev/hermos_rclpy` and the verified ARM64
`build/hermos/device-arm64/lib/{libhermos_heaphook.so,libhermos.so}`. They disable
bridge/discovery and preload that hook. Before loading a module, check
`build/hermos/device-arm64/artifacts.json`, source identity, ELF architecture,
vermagic and the running kernel. The old module inside the nested fork is x86_64.
Load only the matching `hermos_rclpy` module and unload it after all device holders
close and its refcount reaches zero; preserve the pre-existing `hermos` module.
Peer exit alone is insufficient: the forked unlink daemon can retain an inherited
device FD until its polling loop exits. Absent opt-in,
the actual transport and supervisor device tests skip; this is not an equivalent
safety result.

The tracked `session/run_device.py` requires an absolute, unused stage and a
nonempty command. It verifies the artifact/source manifests before loading its
module, waits for zero device holders before one normal unload attempt, and
preserves test and teardown results separately. Teardown failure returns 70.
Set `RCLPY_HERMOS_DEVICE_BUNDLE` to an absolute fresh bundle directory to select
an alternative to `build/hermos/device-arm64`; the same manifest/hash checks
apply. The runner passes that bundle's module to TLD via `RCLPY_HERMOS_KMOD`.
`artifacts.json` must include `devmode` from `modinfo -F devmode` alongside
`vermagic` and `srcversion`. Rebuild older modules without that metadata and
capture fresh source/artifact manifests; do not relabel an old binary.
After matching the loaded module, sysfs identity and node mode, the runner
uses `sudo -n chgrp` only if its newly created device is inaccessible to the
test process. It grants the current primary group access without changing
the mode. A failed grant prevents the test command and still attempts normal
teardown. Pre-existing modules and devices are rejected before loading.
`TMPDIR` is unique to the session and `G3_PEER_LOG_ROOT` holds peer logs outside
pytest's temporary-directory retention. Do not set a shared global
`PYTEST_ADDOPTS=--basetemp=...`: different pytest targets can erase each other's
files. Session runner/protocol regressions are registered in CTest; the previous
runner and evidence under `log/g3/post-import-review-fixes/` remain frozen.

Artifact manifests pin the backend build and workspace source identities.
Checking out the backend submodule does not regenerate a device bundle;
stale bundles must be rebuilt before another device run.

The isolated cleanup and collision executables require exactly one selected case
per process and return an error for an empty filter. CTest registers each filter
explicitly; the runner regression also checks an empty XML cannot mask exit failure.

## Test inventory

The table lists registered test families and their coverage. It is not a
run-result summary; counts and complete versus targeted executions are recorded
in [EVIDENCE.md](EVIDENCE.md).

| Test group | Coverage |
|---|---|
| Metadata reservations | Exclusive generation, stale reader, overflow, dead reader and 1,000 concurrent admission races |
| Producer lifetime | Pending cancel/prepare, active close refusal, single live pool |
| Producer stream mismatch | Rejected preparation preserves a cancellable block without poison |
| Pool retirement and orphan ownership | Pending prepare/cancel, last block after manager, retained reader, competing replacements and preservation of foreign metadata names |
| Existing metadata name collision | First/second block collisions preserve the reopened inode, link count and bytes; owned earlier blocks roll back |
| Mapper shutdown state | Shared seal visibility, moved-from close refusal and fresh epoch only after the final alias disappears |
| Bound default/legacy lifetime | Prepare/cancel/read completion on a CUDA-unused thread, with context restoration |
| Quarantine | Injected `cuEventRecord` failure invalidates the block and blocks 20 replacement pools after synchronization |
| Producer context validation | Primary default allowed; per-thread/foreign contexts rejected |
| Primary release failure | Injected driver failure blocks 20 replacements; one unresolved release attempt |
| Dense descriptor | dtype, span, allocation, layout and overflow validation |
| Producer cleanup faults | Event/query/context/VMM cleanup, partial allocation rollback, context pop failure/mismatch; no cleanup retry or replacement allocation |
| Producer initialisation faults | Granularity; first/second metadata truncate/map; reserve/create/map/access/FD export/event/context failures; CLOEXEC fallback and exported/listener fcntl faults; worker thread and allocation exceptions; socket collision preservation; resource/epoch return and same-manager retry |
| Subscriber completion faults | Record, query, destroy, failed wait plus failed event cleanup; charged reference retention |
| Recoverable subscriber bind faults | Completion creation/wait and their context push failures preserve the synthetic publication for retry and drain |
| Deferred queue and worker | Queue thread/heap failure retains charged reads; the worker completes and waits without heap allocation |
| Subscriber context restoration | Releaser/worker pop gates and faults; no drain before restoration |
| Isolated runner | Missing producer/subscriber case filters return failure despite valid zero-test XML |
| FD receipt and rollback | Real ancillary replies, malformed FD rejection, request socket/fallback/fcntl failures, close/CLOEXEC, retain/push/import rollback and admission return |
| Actual HERMOS processes | Checksum/UID, completion, cleanup, drain, crash/restart, receive/parser/reply errors, accepted-FD fcntl and accept errors, and post-receipt CUDA/bind recovery with H=1 |
| Owned-peer recovery | Host lifecycle/ownership/error regressions; device unpublished-resource stop, reader-crash restart and terminal-poison exit/restart scenarios |
| Private namespace epochs | Worker privilege/ownership and teardown regressions; runtime IPC events, reader crashes, terminal poison and actual CLI automatic restart, with independent healthy peers and unchanged host shm |
| Session tooling (host-only) | Artifact checks, argument rejection, bounded module teardown, exit-code/JSON preservation, peer-log retention and transport statistics protocol |
| Reused and migrated upstream core | Context, pool, publisher message, caches, manager, BufferRef, mapper, read handle and cross-process VMM; three cases require multiple GPUs |
| Test lineage (host-only) | Base revision/hash/inventory, adapted-source ownership, required test classification and CMake registration/failure propagation |

The device-independent `scripts/verify` gate does not run this GPU probe.

### Upstream test migration

`test/upstream/` adapts the pinned MIT test sources to the repaired contracts.
Its [LICENSE](test/upstream/LICENSE) preserves the pinned upstream permission
notice verbatim; the individual source copyright/SPDX notices are retained.
The test adaptation preserves production APIs. All ten upstream
core test groups are now connected to the repaired-core probe; the old Python
bindings and examples remain outside this build.

| Migrated group | Cases | Contract exercised |
|---|---:|---|
| Manager | 7 | Explicit producer stream, completion and exact grace boundary before reuse, cancel-to-publish, successive single-pool epochs and reader-protected reuse |
| BufferRef | 5 | Owned metadata size, committed/stale UID, fresh generation per race and the 100 ms grace boundary |
| Mapper | 4 | Mandatory H/M, missing/stale/unpublished rejection before import, reference and admission return after a real failed FD request |
| Read handle | 6 | Retained primary context for runtime/driver streams, default/legacy acceptance, per-thread rejection; two cases require multiple GPUs |
| VMM | 1 | A spawned child imports, checks all 4,096 bytes and releases; parent and child report final context cleanup |

These five targets contain 23 cases. The five unchanged upstream targets add
29 cases, one requiring multiple GPUs. Read-handle fixtures use synthetic
pointers without dereferencing them; actual import/data coverage comes from the
VMM child and HERMOS transport. VMM's CPU readback is a functional oracle, not
copy-free evidence, and its direct importer does not establish the mapper H bound.

The BufferRef and manager targets wrap libstdc++'s steady-clock entry point at link time in
this static-core test build. Only their grace cases freeze the clock, verify the
committed timestamp, reject at 0/99,999 microseconds and admit at 100,000; other
cases use the real clock. No production clock or grace policy changes.
Metadata fixtures unlink only successfully created names. Rejection tests report
fixture setup failures separately, and the mapper counts actual `connect` calls
so early protocol rejection cannot masquerade as import-failure cleanup.

The tracked [test-lineage lock](lineage/upstream-tests.lock.json) records the
base upstream revision, original hashes and dispositions for all 13 core test
files, including the six adapted sources and the reused VMM protocol header.
`prepare.py` checks it against the verified pristine tree before applying the
core patch. CMake configure repeats the check and derives its ten GTest source
registrations and separate VMM helper from the same lock. A changed base pin,
unclassified/missing original, hash mismatch, duplicate target/adapted path or
missing adapted source fails validation. An intentionally unused file needs a
reason; a `test_*.cpp` cannot silently become a support file. Normal edits to an
adapted source do not require updating its original hash.

The existing archive/whole-tree checks already reject changes to the current pin.
Lineage adds review of adapted sources when that pin changes; it does not make the
repaired tree's own `BUILD_TESTING=ON` compatible. Continue to build its core with
`BUILD_TESTING=OFF` and use this probe. The host-only `test_upstream_lineage` target
checks rejection paths and actual CMake registration without loading CUDA.

### Failure injection and observation limits

Cleanup diagnostics use numeric errno/CUDA codes where formatting an error into
an allocating string could throw during an already-failing `noexcept` cleanup
path. The upstream `posix_error.hpp` helper currently has no callers; its presence
is not a reason to reintroduce allocating error formatting in those paths.
Numeric values and parser-detail fields are not all independently asserted by
the current tests. A passing diagnostic-prefix check has that narrower meaning.

Producer allocation/release call counts do not measure physical VRAM or driver
resource residency. CUDA fault injection skips the selected real call; it tests
the caller's handling of that error, not the driver's internal state after a real
failure. Initialisation checks observe top-of-stack identity and push/pop call
balance; identity alone cannot detect a repeated push of the same context.
Intermediate failing trials include fixture/oracle corrections as well as core
failures. A failed trial is evidence for a core change only when its failure
actually isolates that core behavior; final PASS counts are not a count of
independently reproduced core defects. Revision-specific trials and final runs
remain separate in EVIDENCE.
Metadata truncate/map injection selects successfully created owned FDs and their
inode identity. Socket/bind/listen errors are injected; a separate real collision
case checks preservation of an existing socket name. Dedicated cases exercise
CLOEXEC fallback and fcntl failure. Socket selection observes process-wide AF_UNIX
stream calls after CUDA warm-up; a different driver/MPS configuration needs its
selection validated again. Accepted-FD fcntl faults use static-call link wrapping.
Accept-error injection closes a real accepted FD and returns EINTR/EIO; it tests
the server retry branch without claiming actual kernel resource exhaustion.
The isolated subscriber bind-failure fixtures use synthetic pointers and retain
their publication for retry. Real `BufferMapper::map()` failure destroys that
publication; its retry imports VMM memory and the ready event again. The actual
peer cases below cover that separate rollback and re-import path.
The native FD-receipt fixture uses
`/dev/null` and deliberately fails retain/push/import; it does not claim successful
CUDA import. Its two-header sender may be coalesced into one SCM_RIGHTS array by
Linux. The actual HERMOS peer cases send duplicated VMM FDs or inject `EPIPE`
for three requests, then import the same descriptor and verify the GPU checksum.
These cases cover successful retry after malformed replies and send failure.
Three additional peer cases receive a valid VMM FD before failing primary retain,
context push or VMM import for the first three attempts, then import the same UID
with the same H=1 mapper and verify the GPU checksum. Each rejection checks
CLOEXEC/EBADF, restored context, zero read references, nonpoison and both peers'
settled FD/map counts. Per-map retain/import counts are attempts; push/pop/release
counts include only successful calls. After successful read drain, all-thread
successful retain/release and push/pop counts balance. Successful import may allow
the driver to reuse the received FD number, so that path checks settled FD counts
rather than requiring the old number to remain EBADF.
All nine receipt cases compare the complete VMM field set, requiring zero stage
and cleanup calls for retain/push/import failures. They also require the selected
full diagnostic prefix exactly three times and none of the other eight prefixes.
The Python consumer checks each STATS tag/field count and uses field names;
`BIND_STATS` separately reports completion-event creation attempts/successes,
ready-wait attempts and successful ready/completion event destruction on the map
thread. Existing `RECEIPT_STATS` and `VMM_STATS` fields are unchanged.
Worker polling may add balanced context
push/pop pairs, so drain checks a lower bound rather than an exact pair count.

Four further peer cases fail allocation-granularity lookup, address reservation,
mapping or access setup after a successful VMM import. Each fails three times,
then imports the same UID with the same H=1 mapper and checks checksum/drain.
The map-thread `VMM_STATS` protocol distinguishes successful imports, stage/event-open
attempts and successful unmap/address-free/allocation-release calls. Granularity
and reserve failures release the imported allocation; map failure also frees the
reserved address; access failure additionally unmaps it. No failed attempt opens
an IPC event. Each selected operation has three matching diagnostics.
Every receipt case observes the received FD's first successful `close()` directly.
Post-import failures, like successful imports, allow later driver calls to reuse
the FD number and therefore do not require map-return EBADF. These counters cover
synchronous map/rollback calls, not asynchronous VMM resource residency after drain.
Those seven failure paths stop before binding. Two further cases fail completion event
creation or ready-event wait after real VMM and ready-event import. Each rejects
three maps before the fourth succeeds on the same UID and H=1 mapper, then checks
the GPU checksum and drain. Their synchronous rollback expectations are:

| Observation per failed map | Completion create | Ready wait |
|---|---:|---:|
| Completion create attempts / successes | 1 / 0 | 1 / 1 |
| Ready wait attempts | 0 | 1 |
| Ready event / completion event destroy successes | 1 / 0 | 1 / 1 |
| VMM unmap / address free / allocation release successes | 1 / 1 / 1 | 1 / 1 / 1 |
| Successful context pushes / pops | 3 / 3 | 5 / 5 |
| Primary retain / release successes | 1 / 1 | 1 / 1 |

Both also require one successful VMM import and received-FD close, zero read refs,
nonpoison and unchanged settled FD/metadata-map counts on both peers. The failed
wait does not enqueue consumer work. Each bind case checks its operation-specific
CUDA error diagnostic and the generic bind failure three times. Successful retry
checks event creation/wait before checksum; worker cleanup is observed through
drain and context/ref balance, not the map-thread event counters. Existing
`bad_event` coverage tests IPC event-open rejection and successful retry; its
operation diagnostic is now counted. It does not assert the complete VMM rollback
counter set used by the receipt cases.

## G3 acceptance scope and remaining work

The review-corrected candidate passed the §36 verification for the tested Thor
configuration under the [authoritative recovery contract](recovery/README.md).
The final selected recovery run passed **119 cases**: 107 host and 12 device/OS
scenarios. Combined with the earlier full diagnostic run, the latest coverage is
**86 targets, 357 PASS + 3 multi-GPU SKIP**. This uses the final five target results
in place of their earlier results; it is not a single full run of the final diff.
The CPU verifier separately passed 466 cases with 118 device opt-in skips.
See EVIDENCE §36 and `log/g3/review-fixes/assessment.json` for exact run ownership,
source hashes, preserved failed trials and the remaining unexecuted cases.
Finalize and Git mutations were not run for these changes.
The original requirement remains: pool, IPC mapping/cache, FD and event resources
must not accumulate without bound under repeated execution or crash. The selected
execution boundary combines per-process bounds with complete private-epoch
teardown; the unisolated diagnostic path does not satisfy that boundary.

| Area | Implemented boundary | Current evidence / remaining limit |
|---|---|---|
| Producer and FD paths | Granularity/metadata rollback, checked CLOEXEC, owned socket cleanup, thread/allocation exception rollback and retry tests | The §36 diagnostic run passed on the corrected core, including 37 actual transport and four direct-Group cases inside a separate private test namespace |
| Consumer completion | Context/bind faults, charged queue failures, allocation-free completion worker and asynchronous cleanup tests | The §36 run also covers NOT_READY requeue allocation failure; injection does not model every driver failure combination |
| Diagnostics | Operation-specific errors, nonallocating imported-resource cleanup diagnostics, parser rejection details and preserved mmap errno | Positive/negative diagnostic assertions passed; they do not cover every numeric code or parser-detail field |
| Process recovery | Root-owned bootstrap, account-derived groups, registered direct-child ownership, parent-death termination and verified fresh-namespace restart | Final host tests and 12 device/OS scenarios cover restart and refusal paths; synthetic failure controls do not claim native CUDA daemon behavior on worker death |
| Resource containment | Per-epoch private IPC/mount namespace and tmpfs, complete teardown before retry | Final 23 isolated epochs include six non-GPU failure controls; 13 native unlink daemons exited zero, and a separate synthetic timeout child exited zero after release |

Device-session `metadata_preserved` covers only `rclpy_zc_gpu_v1_*`.
`cuda.shm.*` objects are outside that preservation check. The standalone
[CUDA event probe](residency/event_probe.py) reproduces residue after explicit
runtime IPC-event destruction and process exit, without HERMOS, heaphook or the
repaired core. Runtime-only IPC events reproduce it, so mixing runtime and driver
APIs is not a necessary condition. Pure-driver event and
[VMM allocation/import probes](residency/vmm_probe.cpp) did not add these objects
in the tested sequences. These observations narrow the reproducer; they do not
establish physical GPU residency or prove all full-suite residue has one cause.

Inventories and mode-level results are recorded in EVIDENCE and the
[residency diagnostic guide](residency/README.md). Successful setup established
empty private shm. In completed GPU epochs, runtime-event objects and the HERMOS type
registry remained private until the final holder exited; the kernel then releases
that mount. Both §36 GPU sessions preserved the host inventory of 1,309 CUDA shm
objects / 107,233,280 bytes and unloaded the test module normally at refcount zero.
No driver object was manually deleted,
no device reset was used and no unlink daemon was killed. Namespace IDs can be
reused after destruction; identity alone is not proof that an old epoch remains.

The unpublished three-block inventory is now tested through explicit publisher
SIGKILL and complete group cleanup. Short epochs still do not establish a
long-running epoch or general event/block-count residue bound. The per-epoch tmpfs limit is not a global budget for
concurrent groups. Worker death can leave incomplete cleanup evidence and shared
socket names; parent-death termination is not persistent crash adoption.

These G3 results do not establish physical VRAM/event/VMM accounting, repair
CUDA's internal cleanup, or provide copy profiling. G4 still needs the
Python GPU provider, Tensor aliases and framework-stream lifetime tests. G5 owns
performance, bridge/copy trace and separate Orin/JetPack 6 and Thor/JetPack 7
assessment; Orin/CUDA 12 and native Ubuntu 24.04/Jazzy remain unverified. Their
scope is separate from the current native G3 candidate and does not waive its
resource-lifetime or recovery requirements.

G4 must connect runtime initialization/registration, TERM shutdown deadlines,
mapper/producer/CUDA shutdown order and asynchronous poison exit to the supervisor
contract. The current probe TERM path does not prove orderly Python runtime
shutdown. General ROS launch and inside/outside-epoch DDS with both RMWs remain
integration gates, as described in the recovery README.
The remaining LOW coverage/documentation items and optional design/refactoring
proposals are not all closed by this correction; EVIDENCE records their disposition.
