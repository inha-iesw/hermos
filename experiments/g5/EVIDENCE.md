# G5 initial measurements — 2026-11-01

G5 is **partially implemented and measured**, not accepted as a complete research
milestone. This evidence covers synthetic CPU, bounded GPU and explicit CPU relay
paths on Thor in the adopted registered-peer/private-epoch environment. The research plan's real
VLN/rosbag comparison and 10% performance requirement remain unassessed.

## Environment and reproducibility

- Workspace HEAD at measurement: `e27fa6b82c069e5030f4f5bf82256f56e19d1785`,
  plus the uncommitted scenario sources identified in each provenance record.
- Thor, aarch64, Ubuntu 22.04/Humble container, kernel `6.8.12-1021-tegra`,
  CUDA 13.2, Torch `2.13.0+cu132`. This is not an Orin or native Jazzy result.
- CPU library/provider and GPU/core use separate Release/BUILD_TESTING=OFF
  copy installs. The production heaphook has no observation feature or exported
  test hook. G2 verified device artifacts and the canonical GPU patch are
  unchanged. Neither the GPU public API nor the operational supervisor changed.
- The device session verifies its kernel module and uses fresh private namespaces.
  All three full runs had zero module references at teardown, normal rmmod exit 0 and
  preserved preexisting host sockets, metadata and module state. Every measured
  epoch additionally checks peer/worker/descendant termination.
- `results/protocol.json`, `provenance.json`, raw peer samples, per-case analysis,
  `summary.json` and `device-session.json` preserve successes and failures.
  CPU `report.md` contains all rows and matched-RMW comparisons.
- For `cpu-01` and `gpu-01`, source archives were made after measurement, with
  every archived file checked against its recorded SHA256 (766 and 895 files).
  Later provenance/archive-only edits were reconstructed to the measured hash.
  All recorded installed artifact hashes also still matched at the post-run audit.
  See each `results/evidence-audit.json`.
- Those first two runs did not hash one loaded transitive library,
  `agnocast_cie_config_msgs`; its path was checked but its pre-run contents were
  not recorded. Subsequent runners hash all project dependencies in the production
  prefix and archive sources **before** measurement. The earlier dependency
  identity is not retroactively proven. System ROS/CUDA/Torch libraries are
  environment dependencies, not a complete binary snapshot in these manifests.

## CPU comparison

Protocol and commands: [cpu_latency/README.md](cpu_latency/README.md).
Full run: `log/g5/cpu-01`, 40 cases, 100 warm-up + 1,000 measured sends per case,
100 Hz target, BEST_EFFORT depth 4. Static and dynamic payloads are separated;
each runs with both Cyclone DDS and Fast DDS. The shuffled order was fixed
before observing results.

| Transport | Measured sends | Receipts | Lost | Complete cases |
|---|---:|---:|---:|---:|
| Python Agnocast adapter | 20,000 | 20,000 | 0 | 20 / 20 |
| Stock rclpy DDS | 20,000 | 19,467 | 533 | 12 / 20 |

All eight incomplete DDS cases were at 1 MiB or 4 MiB. The full command correctly
exited **2**, after saving all 40 results; this was receipt loss, not a teardown
failure. The eight affected comparisons are invalid. Among the 12 complete
matched pairs, the direction depends on the boundary:

| Metric | Agnocast lower | Agnocast higher |
|---|---:|---:|
| Publish→callback mean | 8 | 4 |
| Publish→callback p99 | 4 | 8 |
| Fill→consumer mean | 2 | 10 |
| Fill→consumer p99 | 1 | 11 |

Borrow/resize/fill precedes the publish timestamp, while DDS serialization occurs
inside publish. The separate fill boundary is essential to interpreting these
results. A single bounded run does not establish general superiority or the
research performance requirement. Expanded reports from the original summaries
are in `log/g5/review-reanalysis/{cpu,gpu}/report.md`, with input hashes in
`inputs.json`; the original reports and raw measurements remain unchanged.

Examples below are mean / p99 publish-to-callback latency, in microseconds.
Each row has all 1,000 receipts on both transports.

| Layout / size / RMW | Agnocast | DDS |
|---|---:|---:|
| Dynamic / 1 KiB / Cyclone | 203.29 / 836.46 | 232.88 / 628.14 |
| Dynamic / 1 KiB / Fast DDS | 285.03 / 710.93 | 185.16 / 601.47 |
| Static / 4 KiB / Cyclone | 163.06 / 544.84 | 247.86 / 637.13 |
| Dynamic / 64 KiB / Cyclone | 409.14 / 1081.26 | 431.30 / 805.81 |

These are single bounded runs with process scheduling noise. Mean and p99 can
move in different directions. The paced observed delivery rate is not maximum
throughput. RSS/HWM/FD snapshots are not proof of no allocator leaks.

## Bounded GPU transmission

Protocol and commands: [gpu_latency/README.md](gpu_latency/README.md).
Full run: `log/g5/gpu-01`, 64 warm-up + 1,000 measured sends per size,
50 Hz target, 16 producer blocks, explicit producer/consumer CUDA streams.

All **5,000 / 5,000** measured tensors were received with valid sequence and
whole-tensor checksum. Both peers finished each case with active owners,
pending owners equal to zero. The recorded `wrapper_events` value is an alias
of active owners, not an independent count of CUDA events. Sampled FD counts remained
publisher 106 / subscriber 76 between warm-up completion and the final snapshot.
The command and device session exited 0.

Values below are milliseconds. GPU-done includes mapping/import, checksum
execution and the host event wait. Callback entry follows descriptor dispatch
and lightweight lease construction, before `as_tensor()` maps GPU storage.

| Tensor bytes | Mean publish call | Mean publish→callback | Mean publish→GPU-done | p99 publish→GPU-done | Mean fill→GPU-done |
|---|---:|---:|---:|---:|---:|
| 1 KiB | 3.586 | 3.806 | 6.161 | 7.905 | 6.438 |
| 4 KiB | 3.685 | 3.820 | 5.195 | 8.300 | 5.503 |
| 64 KiB | 3.858 | 4.071 | 5.440 | 7.182 | 5.743 |
| 1 MiB | 3.314 | 3.466 | 5.486 | 7.083 | 5.791 |
| 4 MiB | 3.628 | 3.785 | 6.139 | 8.827 | 6.406 |

The mean publish-call / mean publish→callback ratios are 94–96%. These are
observed overlapping intervals, not an additive attribution of transfer cost:
a callback can begin before publish returns. `publish()` includes prepare,
descriptor allocation/writes, CPU publish and cleanup. Their internal costs
have not been separately measured; worker-round-trip dominance remains an
inference and no optimization was made on that assumption.

The consumer performs two scalar DtoH diagnostics (16 bytes) after its timed
GPU completion. There is no same-core DDS descriptor baseline in this run;
these values do not establish relative GPU performance. CPU and GPU timings
also have different completion work and size accounting, so are not comparable
as transport-only measurements. The G4 small-payload nsys audit is prior evidence;
a new large-payload copy trace has not been collected here.

RSS increased slightly during sample recording (publisher roughly 300–320 KiB,
subscriber roughly 156–176 KiB). Record-list growth and runtime caches are not
separated, so neither this growth nor zero owner counters establishes a leak
verdict. Physical VRAM, core cache/event live/high-water and fragmentation still
need their own observability and sustained-load protocol.

## Matched GPU descriptor comparison after the review

Full run: `log/g5/gpu-compare-01`, using the new shared-typesupport production
prefixes for **both** descriptor paths. Twenty conditions (five sizes, both RMWs,
two transports) each excluded 64 warm-up sends and measured 1,000 at 50 Hz.
All **20,000/20,000** measured tensors passed checksum and receipt checks.
All peer/descendant teardown, owner drain and source/artifact identity checks
passed; the session exited 0 with module refcount 0, normal unload and preserved
host state. The earlier `gpu-01` timings are not mixed into this comparison.

In this single bounded run, Agnocast's mean publish→GPU-done latency was higher
in **all ten matched pairs**, by **79.83–281.97%**. Mean publish-call duration was
2.741–3.873 ms for Agnocast and 0.166–0.225 ms for DDS. These values support further
investigation of the publisher path; they do not isolate prepare from metadata
writes or prove a transport-only cause.

Examples (milliseconds, all 1,000 measured receipts on each path):

| Size / RMW | Agnocast mean publish→GPU-done | DDS mean publish→GPU-done | Additional latency |
|---|---:|---:|---:|
| 4 KiB / Cyclone | 4.420 | 2.458 | +79.83% |
| 4 KiB / Fast DDS | 5.583 | 1.462 | +281.97% |
| 1 MiB / Cyclone | 5.309 | 1.556 | +241.22% |
| 1 MiB / Fast DDS | 5.431 | 1.675 | +224.23% |

All boundaries, including p99 and fill→GPU-done, are retained in
`log/g5/gpu-compare-01/results/report.md` and raw per-peer records. Both paths use
the same CUDA core, pool, DLPack owners, Torch fill/checksum and stream protocol.
The comparison includes descriptor API, serialization and heap integration.
It establishes functional comparison coverage, **not a performance improvement**,
real VLN acceptance, a host-staging baseline or sustained-load leak freedom.

## Explicit CPU relay

Protocol and commands: [cpu_relay/README.md](cpu_relay/README.md).
Full run: `log/g5/relay-01`, dynamic 1 KiB/64 KiB/1 MiB, both RMWs, 100 warm-up
and 1,000 measured sends per condition at 50 Hz. Source, relay and sink are three
registered processes in the same private epoch. All paths, including the
DDS→DDS control, explicitly copy the payload into a fresh outgoing message.

| Path | Measured sends | Sink receipts | Lost | Complete cases |
|---|---:|---:|---:|---:|
| DDS→DDS control | 6,000 | 5,786 | 214 | 4 / 6 |
| Agnocast→DDS | 6,000 | 5,951 | 49 | 4 / 6 |
| DDS→Agnocast | 6,000 | 5,976 | 24 | 4 / 6 |

All 12 small-payload cases received every measured message. All six 1 MiB cases
had receipt loss, so the full matrix correctly exited **2**. The two mixed paths'
loss was located using relay sequence records: Agnocast→DDS reached the relay
without gaps and lost 49 receipts afterward; DDS→Agnocast lost 24 measured
receipts before reaching the relay, with the same count at the sink.
This locates observed gaps without asserting their underlying cause.

For the eight valid mixed-path comparisons, mean end-to-end latency was lower
than the matched DDS→DDS relay in two and higher in six. For example, 1 KiB
Fast DDS control / Agnocast→DDS / DDS→Agnocast measured 536.92 / 567.90 / 658.68 µs.
All rows and matched comparisons are in `results/report.md`; per-hop values and
relay entry/publish/return timestamps remain in the raw records. Direct CPU
results use a different rate and hop count and are not substituted as a matched
relay control. No accepted overhead bound is defined by this synthetic run.

The runner checked unchanged source/artifact identity, all three peer exits and
natural descendant cleanup for every case. `relay-01` archived sources before
measurement, including the broadened dependency hash inventory. This does not
enable the native auto-bridge, external-namespace communication or GPU bridging.

## Validation and retained unsuccessful runs

`CMAKE_BUILD_PARALLEL_LEVEL=2 PYTHONDONTWRITEBYTECODE=1 bash scripts/verify`
completed with 14 packages built and **534 PASS + 118 device opt-in SKIP**,
zero failure/error, across 40 CTest targets. The new CPU analysis/protocol/event/
provenance suite contributes 62 passes. Log: `log/g5/verification.log`.
The GPU smoke and full sessions above are additional device executions, not
part of that host test count. This is not a new finalize result.

After adding the relay scenario, the benchmark adapter was reconfigured/built
and both registered CTest targets passed: **88 scenario tests**, comprising the
62 CPU checks and 26 additional relay checks. Logs are
`log/g5/relay-host-verification.log` and `log/g5/scenario-verification.log`.
Together with the unchanged host suites above, the verified inventory is
**560 PASS + 118 device opt-in SKIP across 41 targets**. The full verifier ran
before the relay target was added; the new target was then checked separately.
All G5 Python files at that stage passed flake8. These are historical counts;
the review-fix verification below supersedes them for the current tree.

Initial CPU trials are retained, not overwritten:

| Stage | Outcome and correction |
|---|---|
| `smoke-01` | Invalid numeric ROS topic token; added a letter prefix. |
| `smoke-02` | Shared heap path incorrectly classified as a library; filter corrected, regression tests added. |
| `smoke-03` | Humble stock subscription lacks `get_publisher_count`; use Node graph query. |
| `smoke-04` | Large DDS payload loss exposed insufficient warm-up/early matrix exit; fixed protocol before full run. |
| `smoke-05` | Full-payload oracle across all 40 cases; 4 MiB static Cyclone DDS lost 10/16 measured receipts, exit 2. |
| `gpu-smoke-01` | Five sizes, full GPU checksum, successful drain and exit 0. |
| `relay-smoke-01` | DDS→Agnocast wrote a field before resizing; reordered PREPARE/resize before writes. Failure cleanup succeeded. |
| `gpu-compare-smoke-01` | Fast DDS endpoint creation failed because the old message build had only static typesupport; epoch failed, cleanup completed, no comparison summary. The failed stage is preserved. |
| `relay-smoke-02` | All 288 measured receipts arrived; 3 warm-up relay rows were missing in 2 cases, which correctly invalidated those chains and returned exit 2. |

The revised CPU warm-up was 100 for both `smoke-05` and `cpu-01`; measured DDS
loss was retained. No timeout, receipt loss or peer failure was relabeled PASS.

## Review corrections and current verification

The external review `code-review-20261101-162641.md` was applied to the G5
experiment layer. The corrected GPU build uses fresh `gpu-comparison-core` and
`gpu-comparison` build/install prefixes: ROS Fast DDS typesupport is shared, while
the repaired core remains static. Old build directories and the failed
`gpu-compare-smoke-01` are retained. Production identity now requires these
artifacts, includes C/header/template/Cargo inputs, rejects test instrumentation
and checks mapped libraries against the selected prefixes.

Unmeasured epoch/matrix control is shared across CPU/GPU/relay. Complete loss
retains explicit counts and N/A receipt latency, continues all cases and returns
2; malformed evidence and peer failure still stop immediately. Teardown requires
exact integer-zero peer/descendant statuses. Frozen protocol values drive GPU
runtime/endpoint budgets and relay cases. Warm-up resource snapshots record the
first received sequence at or beyond the boundary. The rate estimator now uses
N−1 intervals over first-to-last measured callbacks; original summaries retain
their original estimator and are not overwritten.

CPU and GPU reports now expose all relevant boundaries and use the same sign:
positive means additional latency. GPU DDS endpoint tests use the actual
`GpuRuntime` with fake native ownership, plus real RMW endpoint creation. The
`wrapper_events` field is retained only as an owner alias, not event evidence.
The measured fill/publish/callback code stays inline; extracting it or optimizing
metadata writes before separate internal timing would change the measurement.
General registration/package migration remains outside this benchmark-only fix.

Current full verification command:

```bash
CMAKE_BUILD_PARALLEL_LEVEL=2 PYTHONDONTWRITEBYTECODE=1 G5_VERIFY_GPU_HOST=1 bash scripts/verify
```

It completed with 14 packages, **43 CTest targets / 758 cases = 640 PASS +
118 device opt-in SKIP**, zero errors/failures. The explicit GPU host gate added
20 endpoint tests (52 executed, including 32 pure tests already counted above),
for **660 distinct PASS + 118 SKIP** at that run. After the read-only review
cross-check identified unsupported relay layout/transport declarations, four
failing regressions reproduced the issue. The fixed three-path experiment now
rejects those declarations. Five added cases (including valid reversed transport
order) passed with all four G5 CTest targets; current G5 is **193 PASS**, yielding
**665 distinct PASS + 118 SKIP** with the unchanged suites above. Logs:
`log/g5/review-scenario-verification.log` (173 pure tests) and
`log/g5/review-scenario-ctest.log`. The GPU endpoint gate contributes the remaining
20 G5 tests. No measurement code changed during a device run.
Log: `log/g5/review-verification.log`. All G5 Python files pass flake8 at width 100;
shell entry points pass `bash -n`. This is current verifier evidence, not a
`/finalize` result or a repeat independent code review.

The corrected comparison smoke `log/g5/gpu-compare-smoke-02` completed all
20 conditions (five sizes × two RMWs × two descriptor transports): **320/320**
measured tensors, full checksum, no receipt loss, source/artifact identity stable.
Every epoch passed peer/descendant teardown and owner drain; the device session
returned 0 with zero module references, normal unload and host state preserved.
This verifies the formerly failing Fast DDS path and the previously unmeasured
Cyclone DDS descriptor path on Thor.

Remaining review work: split GPU publish internal costs before deciding a worker
batching optimization; independently instrument core CUDA events/cache/VRAM;
and decide installable supervisor/registration ownership outside this experiment.

## Remaining acceptance work

| Research-plan area | Current evidence | Still needed |
|---|---|---|
| §6.1 profiling | Synthetic protocols, raw timings, process snapshots and reports | Actual edge VLN pipeline and full copy/memory observability |
| §6.2 communication | CPU static/dynamic two-RMW comparison; same-core GPU descriptor comparison across five sizes/two RMWs | Native C++/host-staging baselines, sustained load and leak/allocation-failure criteria |
| §6.3 Bridge | Explicit CPU relay, both directions/two RMWs; 12/18 complete synthetic conditions | Automatic bridge lifecycle/plugin adoption, external/GPU bridge, large-payload delivery and accepted overhead bounds |
| §6.4 end-to-end | No real-workload acceptance result | Rosbag, executable VLN graph, >=10% agreed comparison and node-failure pipeline continuity |
| Platform coverage | Thor/Humble container | Orin/CUDA 12 and native platform evidence |

The user confirmed on 2026-11-01 that no real rosbag/VLN pipeline is currently
available and only Thor is accessible, and directed synthetic evaluation first.
Real-workload and Orin acceptance are therefore waiting for external inputs;
the active evaluation scope is Thor synthetic. The current registered group
restart policy is not proof that an actual VLN pipeline remains uninterrupted.

Against the schedule in §7 of the year-one plan, this work is within
**integration/validation (September–November): initial performance evaluation**.
It does not finish real VLN node integration, stability acceptance or the final
year-one report. The next technical work is GPU publish internal-cost diagnosis, a native C++
CPU baseline and resource/copy observability;
real-workload and Orin branches additionally require the missing external inputs.
