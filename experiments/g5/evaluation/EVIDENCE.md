# Independent G5 evaluation — 2026-11-03 KST

This review initially used local G5 evidence. The user subsequently supplied the
latest Claude artifact as `.cache/HERM0S 전송 벤치마크.html`; its raw experiments
were then located in the previous evaluator's scratch directory. The
[external review](EXTERNAL_REVIEW.md) independently checks those newer fanout,
host-staging/C++ and reclaim results. The historical G5 results below retain their
own measurement scope and are not substituted for that newer evidence.

## Historical evidence, different questions

Reproduction: the [evaluation CLI](README.md) reads `log/g5/cpu-01/results`,
`log/g5/gpu-compare-01/results`, and `log/g5/relay-01/results`. The validated
reanalysis is `log/hermos/g5/evaluation-legacy-02/`; input hashes are retained in
`evaluation.json`. Original transport names, files and failed verdicts remain.
The [mean/tail comparison figure](../../../log/hermos/g5/evaluation-legacy-02/completion-comparison.png)
is also available as a [PDF](../../../log/hermos/g5/evaluation-legacy-02/completion-comparison.pdf).

| Run | Received / expected | Lost | Longest consecutive loss | Valid paired conditions | Shared-heap path faster: completion mean / p99 |
|---|---:|---:|---:|---:|---:|
| CPU | 39,467 / 40,000 | 533 | 191 | 12 / 20 | 2 / 1 |
| GPU | 20,000 / 20,000 | 0 | 0 | 10 / 10 | 0 / 0 |
| CPU relay | 17,713 / 18,000 | 287 | 68 | 8 / 12 | 1 / 0 |

Completion includes allocation/borrow/fill through consumer work. CPU/relay main
runs check payload endpoints, whereas GPU completion includes its synchronized
checksum. They are distinct workloads; this table does not compare CPU versus GPU.
Relay has two proposed paths per DDS-to-DDS control. Incomplete pairs are invalid,
not silently excluded to improve a pooled effect.

The CPU result illustrates why publish-to-callback alone is insufficient: its
mean favored the shared-heap path in 8 valid pairs, but completion mean only in 2.
Tail behavior is weaker still. Loss is also bursty: 191 consecutive missing
messages is a service gap that a single loss percentage hides.

In the original GPU comparison, publisher process CPU cost after warm-up through
finish/drain was 3.639–4.042 ms/send for the legacy shared-heap descriptor path
versus 1.060–1.356 ms/send for DDS. Voluntary context switches were respectively
285.302–290.626/send and 4.456–4.553/send. These support the synchronization-cost
hypothesis; they do not measure internal worker command counts or isolate pure
message processing from drain/control work. `cpu-01` lacks these counters.

The offered period is a useful diagnostic budget, not an application deadline.
For example, 4 MiB DDS received messages exceeded the 10 ms fill-to-consumer
budget 837 times (static/Fast DDS), 857 times (static/Cyclone), and 943 times
(dynamic/Cyclone). Those cases also lost messages, so paired latency effects
remain invalid. Neither paced receipt rate nor this budget proves saturation
throughput or VLN deadline satisfaction.

## New-name functional validation and instrumentation

`log/hermos/g4/rename-device-01/` passed 31 tests (21 device plus 10 host helpers).
Its runner exited 0, normally unloaded `hermos_rclpy`, and preserved pre-existing
module/socket/metadata state. This validates the new-name bundle under the
existing private-epoch contract; it does not extend the supported platforms.

The first P0 host gate passed 825 cases: 707 PASS and 118 device opt-in SKIP, zero
failures/errors. It built 14 packages and ran 44 CTest targets. Subsequent P0
changes use the G5 targets again; this count belongs to that first gate snapshot.

`log/hermos/g5/p0-cpu-smoke-01/` checked full payload contents: HERMOS received
320/320 and DDS 287/320. Five large DDS conditions lost messages, so the run exited
2. All peers and the owned module were normally reclaimed. It is **not** a passing
matrix or a valid latency baseline.

That smoke exposed measurement interference: detailed memory/thread snapshots
took about 14–28 ms in an inspected peer, longer than the CPU 10 ms period.
Those snapshots were outside individual timestamp intervals but still disturbed
pacing. The original smoke is preserved as diagnostic evidence. The implementation
now takes only light counters at warm-up/final-ID boundaries by default; detailed
collection there requires `--resource-diagnostics`. Full before-start/after-drain
snapshots remain. Post-collection start counters exclude the observer's own CPU
cost, and late warm-up rows are counted explicitly in resource denominators.

New CPU/GPU comparisons support independent alternating repeats. DDS/HERMOS
order reverses each repeat; pair groups are seeded-shuffled. Reports match the
same repeat and retain every loss. Current clocks remain adaptive (`schedutil`,
CPU allowed set 0–13), with actual thread affinity and available frequency state
recorded. These are not fixed-clock or energy-efficiency measurements.

## P1/P2 implementation and verification

GPU descriptor metadata now completes structural resizes on the publisher worker,
then uses caller-thread scalar writes and scoped bulk access for the event handle
and UTF-8 socket path. Generation, creator-thread, strict integer/bool validation
and writable-export checks remain. A descriptor failure after GPU preparation
still must not cancel a committed GPU generation prematurely.

The P1/P2 host verification built 14 packages and passed the regular 44 CTest
targets: 841 cases, **723 PASS + 118 opt-in SKIP**, zero failures/errors. Its
additional production-artifact GPU host gate passed **64 tests**, partly
overlapping the regular suite. The subsequently added external-audit checks
passed the focused evaluation target, **30 tests** including eight new cases.
These counts are not a sum of distinct tests. The
[full host log](../../../log/hermos/g5/p1p2-comparison-01/host-verification.log)
is preserved; focused evaluation logs are in
`build/hermos/verify/g5_benchmark_adapters/ament_cmake_pytest/`.

`log/hermos/g4/p1p2-device-01/` passed **31 tests** and its separate copy trace;
the runner exited 0 and normal owned-resource cleanup passed. The trace covers
eight uint8 transfers in marked publish/consume regions; startup is excluded,
scalar diagnostics remain, and UM tracing was unavailable. It is neither a
physical-VRAM measurement nor a performance result. The original external CPU
signal failures are preserved; their subsequent targeted fix is described below.

## CPU signal shutdown follow-up

The unchanged lifecycle suite happened to pass in
`log/hermos/cpu-lifecycle-reproduce-01/`; that alone did not resolve the prior
intermittent failures. A controlled host regression shut down a real stock Humble
context immediately before native WaitSet construction. It reproduced the same
`RCLError` escaping the test peer's executor thread.

The test peer now marks the requested signal-wait phase and accepts only that
specific invalid-context WaitSet error when the context is already shut down.
Other native errors, active-context failures and allocation errors remain failures.
Production Node/runtime shutdown behavior is unchanged. The controlled race and
nine allow/deny cases pass through the new `test_typed_signal_shutdown` CTest target.

`log/hermos/cpu-lifecycle-fixed-01/` then passed **26 device tests**: six typed
system, nine facade system and eleven lifecycle tests, including SIGINT/SIGTERM,
retained alias checks, surviving peer progress and endpoint/process replacement.
The runner exited 0, unloaded its module normally and preserved pre-existing
resources. XML results and the tested helper hash are retained in `test-results/`
alongside the ten host regression results. This resolves the identified test-peer
exception-classification gap; the older failed logs remain failed historical runs.

## P1/P2 device performance: qualified post-run comparison

The new-name pre-optimization baseline is `log/hermos/g5/p0-gpu-baseline-01/`:
60 conditions, 60,000/60,000 receipts, runner exit 0, stable identity and normal
cleanup. `log/hermos/g5/p1p2-gpu-01/` completed the same 60 conditions and
60,000/60,000 receipts with no offered-20-ms-period exceedance. Its **runner
exited 1** at the final identity comparison because a concurrent documentation
commit changed Git HEAD from `7e8c9d5` to `ad65a39`. That original failure and
missing final `summary.json` are preserved, not converted into automated PASS.

A [post-run identity audit](../../../log/hermos/g5/p1p2-comparison-01/identity-audit.json)
under the original environment/affinity found **only `git_head` changed**. All
931 source hashes, 113 CPU artifact hashes and 48 GPU artifact hashes matched,
and the source archive matched its starting manifest. The commit changed only
README/developer guides. All case teardowns passed; the owned module unloaded
normally and pre-existing resources were preserved. This supports descriptive
analysis of the completed measurements, subject to this post-hoc qualification;
the strict whole-run gate remains failed.

Both phases used the same CUDA IPC payload implementation, five sizes, two RMWs,
50 Hz, 64 warm-up plus 1,000 measured messages, and three alternating DDS/HERM0S
repeats. The CPU installed artifacts are identical across phases; the sole GPU
installed-artifact difference is `endpoint/metadata.py`. The DDS timed descriptor
operations do not execute the changed metadata functions. P1 and P2 were applied
together, so their individual contributions are not isolated.

| HERM0S metric | Before | After P1/P2 | Matched run-change median |
|---|---:|---:|---:|
| Mean publish call | 3.617 ms | 0.357 ms | −90.1% |
| Mean fill→GPU-done completion | 5.898 ms | 2.547 ms | −56.9% |
| Completion p99 | 7.829 ms | 4.263 ms | −45.1% |
| Publisher CPU per observed row | 4.016 ms | 1.374 ms | −65.1% |
| Publisher voluntary switches per observed row | 289.632 | 14.448 | −95.0% |

Before/after cells are medians of 30 **run-level** statistics spanning all
size/RMW/repeat conditions, not a pooled-message distribution. The
[full comparison](../../../log/hermos/g5/p1p2-comparison-01/report.md) preserves
each of ten size/RMW groups with its three-run median/range, and
[JSON](../../../log/hermos/g5/p1p2-comparison-01/comparison.json) retains every
repeat and counter-window denominator. All new resource windows contain 1,000
observed/measured rows, zero late warm-up rows, complete final-ID windows, and
exclude warm-up collection cost. Publish means range 0.300–0.393 ms after P1/P2;
the original approximately 0.3 ms prediction was not a guaranteed threshold.

The HERM0S completion mean fell **36.2–71.2%**, and p99 **27.5–81.1%**, across
all 30 ordinal-matched pre/post comparisons. The retained 4 KiB/Cyclone baseline
repeat-1 outlier contributes to the largest reduction. After optimization,
HERM0S beats contemporaneous DDS in **13/30 mean** and **8/30 p99** comparisons,
versus zero in the baseline. Its median HERM0S/DDS ratio is still **1.041 for
mean and 1.111 for p99**. The overhead reduction is substantial; a consistent
latency advantage over DDS is not established.

Pre and post are separate chronological phases, not randomized optimization
crossovers. DDS completion-mean drift ranges −27.6% to +46.6% (median +5.6%);
p99 drift ranges −39.2% to +69.4%. Adaptive clocks and thermal/power state remain
confounders. Ratio-of-ratios is retained as a descriptive control adjustment,
not proof that these influences were removed. The sender stayed near 50 Hz;
neither that paced rate nor the period diagnostic establishes peak throughput.

The [comparison figure](../../../log/hermos/g5/p1p2-comparison-01/comparison.png)
also has a [PDF](../../../log/hermos/g5/p1p2-comparison-01/comparison.pdf).
Reproduction: run the evaluation CLI on the preserved after-results into
`log/hermos/g5/evaluation-p1p2-01`, then the comparison directory's `compare.py`
and `plot.py`. The original failed-run status and identity qualification must
accompany exported results.

## Strict uninstrumented GPU follow-up

`log/hermos/g5/gpu-strict-01/` completed the full 60-condition matrix with
**60,000/60,000 measured receipts** and runner exit **0**. It retained the five
sizes, two RMWs, 50 Hz, 64 warm-up plus 1,000 measured messages and three
alternating transport repeats. Start/end provenance is identical, including Git
HEAD; checksums, owner drain, all case teardowns and normal module unload passed.
The [raw report](../../../log/hermos/g5/gpu-strict-01/results/report.md) and
[independent reanalysis](../../../log/hermos/g5/evaluation-strict-01/report.md)
are separate from the earlier failed identity-gate run, whose status is unchanged.

All 30 matched pairs are valid. HERM0S has lower completion mean in **10/30**
pairs and lower p99 in **11/30**; median paired HERM0S/DDS ratios are **1.172**
and **1.178**, respectively. Across the 30 HERM0S runs, median publish-call mean
is **0.346 ms** (run range 0.288–0.403 ms), median completion mean **2.502 ms**,
and median completion p99 **4.133 ms**. These are run-level statistics, not
pooled-message quantiles. Metadata overhead remains reduced, but this matrix
does not establish a consistent latency advantage over DDS.

Actual sender rates range 49.99976–50.00017 Hz; no received completion exceeds
the offered 20 ms period. That is a paced synthetic diagnostic, not saturation
throughput or a VLN deadline result. Adaptive clocks, scheduling and chronological
optimization phases still limit causal attribution. This strict PASS resolves
the missing whole-run acceptance evidence, not those broader limitations.

## Follow-up GPU cost diagnosis

`log/hermos/g5/gpu-costs-01/` passed eight diagnostic conditions, 200 measured
messages each: 1 KiB/4 MiB, both RMWs and both descriptor transports. Source/end
identity matched, checksums and owner drain passed, and the runner exited 0 with
normal cleanup. The [stage report](../../../log/hermos/g5/gpu-costs-01/results/report.md)
and [usage/boundaries](../gpu_latency/costs/README.md) keep these instrumented
timings separate from headline comparisons.

HERM0S mean prepare was 18–22 µs, descriptor fill (including resize) 222–252 µs,
decode 96–100 µs and native map 445–498 µs. DDS map means ranged 472–1,185 µs;
some conditions had long tails, while the same core path was used. At 4 MiB the
existing GPU completion wait averaged 885–1,004 µs across both transports.

The adopted core has no idle import cache: every map obtains a block reference,
attaches metadata, obtains a fresh FD through a blocking Unix socket exchange,
imports/maps CUDA memory and opens/creates the relevant events. The measured
wrapper also includes Python/native conversion, owner polling, locks and GIL
reacquisition. There is no payload memcpy in this map path. Current read cleanup
is deferred until its bound work and aliases finish, outside the measured map.
The observed stage does not isolate socket, CUDA driver or scheduling costs, and
its DDS/HERM0S difference cannot be assigned to DDS itself. No cache/lifetime
policy change was inferred from this diagnostic.

The follow-up full host gate passed **783 tests with 118 opt-in skips** (901 total),
plus a separate overlapping **90-test GPU production host gate**. After a fanout
smoke exposed a final control-round timeout, the narrow deadline fix passed the
registered **45-test fanout target**. Full-gate logs/XML are retained in
`log/hermos/g5/followup-host-verification-01/`.
The [final experiment identity audit](../../../log/hermos/g5/followup-host-verification-01/experiment-identity-audit.json)
also verifies each of the four follow-up GPU runs' 947 archived source hashes,
start/end identity, expected exit status and normal cleanup. It preserves the
4K fanout delivery failure rather than converting it into PASS.

## Repeated Python GPU fanout

The [fanout scenario](../gpu_fanout/README.md) compares pageable
GPU→host→stock rclpy DDS→GPU staging against the HERM0S CUDA IPC payload path.
Both use the same ROS timer pacing, full uint8 GPU checksum and post-timestamp
scalar diagnostics. This differs from the same-CUDA-core descriptor comparison
above. Allocation, staging and conversion policies are part of the comparison.

`log/hermos/g5/gpu-fanout-01/` completed all 12 conditions: 4K RGB
(24,883,200 bytes), eight subscribers, Fast DDS, BEST_EFFORT depth 4, eight pool
blocks, 64 warm-up sends plus 300 measured sends, 5/30 Hz and three adjacent
alternating transport repeats. Source/artifact identity and every teardown passed.
The runner correctly exited **2** for receipt loss. HERM0S received
**14,400/14,400** measured receipts; vanilla DDS received **7,452/14,400**.
All six paired latency comparisons are invalid because of DDS loss.
The [full report](../../../log/hermos/g5/gpu-fanout-01/results/report.md) retains
every subscriber's receipts and each separate repeat.

| Offered Hz | Payload path | Receipts per repeat / 2,400 | Worst-reader loss range | Common IDs per repeat / 300 | Actual sender Hz |
|---:|---|---|---:|---|---:|
| 5 | Vanilla DDS staging | 1,090 / 1,078 / 1,066 | 62.33–70.00% | 1 / 0 / 0 | 5.000 |
| 5 | HERM0S CUDA IPC | 2,400 / 2,400 / 2,400 | 0% | 300 / 300 / 300 | 5.000 |
| 30 | Vanilla DDS staging | 1,372 / 1,393 / 1,453 | 48.33–51.33% | 14 / 20 / 15 | 22.148–22.651 |
| 30 | HERM0S CUDA IPC | 2,400 / 2,400 / 2,400 | 0% | 300 / 300 / 300 | 30.000 |

For HERM0S, all-reader completion means range **27.615–28.382 ms at 5 Hz** and
**17.388–17.433 ms at 30 Hz**; corresponding p99 ranges are **37.900–47.332 ms**
and **18.544–18.919 ms**. All 900 common IDs at each rate complete within that
rate's offered period. Lower offered rate did not produce lower observed latency;
adaptive clocks and scheduling were not isolated in this experiment.

Vanilla DDS's 30 Hz all-reader means of 62.134–65.075 ms are conditional on only
14–20 common IDs. At 5 Hz, just one common ID exists across three runs; two runs
have no all-reader latency sample. Those observations cannot support a paired
speedup claim or a stable tail estimate. Lowering the offered rate to 5 Hz did
not establish a no-loss 4K condition. Loss causes and a load threshold remain
unisolated; 30 Hz also produced unequal achieved sender rates.

The separate **64 KiB / 5 Hz control**, `log/hermos/g5/gpu-fanout-small-01/`,
kept eight subscribers, QoS, pool/read limits, 64 warm-up sends, 300 measured sends
and three alternating repeats. All six conditions passed with **7,200/7,200
receipts per transport**, stable source/artifact identity, normal cleanup and
runner exit **0**. Every reader received every measured ID; both sender rates
were approximately 5 Hz. The
[control report](../../../log/hermos/g5/gpu-fanout-small-01/results/report.md)
retains all three valid paired effects.

| 64 KiB payload path | All-reader mean range | All-reader p99 range |
|---|---:|---:|
| Vanilla DDS staging | 9.054–9.075 ms | 9.500–9.534 ms |
| HERM0S CUDA IPC | 6.583–7.710 ms | 8.865–9.582 ms |

HERM0S all-reader mean is **18.63%, 15.04% and 27.29% lower** in repeats 1–3.
Its p99 changes are **−3.19%, +0.85% and −7.02%**, so the tail does not improve
in every repeat. All 900 common IDs per transport meet the offered 200 ms period.
This establishes a bounded no-loss comparison for the smaller payload, not a
no-loss 4K result or a pure rate effect. It changes the payload path, including
staging and allocation costs, and does not establish the research's real-VLN
10% requirement. Three runs and 300 samples per run still limit tail inference.

The first smoke, `gpu-fanout-smoke-01`, stopped on a final COUNT-response deadline
before peer reports were saved. The control deadline was corrected without
changing payload delivery: five seconds for starting drain rounds, plus a shared
one-second reply budget per round. `gpu-fanout-smoke-02` then saved both conditions
and exited 2 for 39 actual missing DDS receipts; cleanup and identity passed.
Both original smoke outcomes remain preserved.

Warm-up counts publisher sends, not guaranteed warm-up receipts. Peers save
sequentially after drain, so final receipts do not share one absolute cutoff.
Completion starts at actual fill, excluding earlier timer lateness; diagnostics
after its timestamp still affect subsequent consumer work and loss. All-reader
completion is an application-relevant view when every branch needs the same ID,
not a universal consumer contract. No VLN deadline, saturation throughput,
physical-memory or long-run stability claim follows from these bounded runs.

## Remaining evaluation and acceptance axes

The external HTML supplies initial fanout, C++/host-staging and reclaim A/B
evidence. The following items concern remaining validation, rather than absence
of every preliminary experiment; see the external review for specific findings.

Current scope (2026-11-03 KST): CPU reclaim policy changes and additional P3
A/B or reclaim-specific long-run experiments are deferred at the user's request.
Existing reclaim measurements remain historical evidence, not an active next step.

- Additional independent repeats and randomized optimization crossovers.
  The strict three-repeat whole-run PASS above does not turn message-level
  observations into independent replications or isolate P1 from P2.
- CPU full-consume latency at the same protocol, separate from endpoint checks.
- Sustained offered-load and subscriber-count sweeps, slow consumers/backpressure,
  retained aliases and concurrent publishers beyond the bounded fanout above.
- Large/small/large memory reuse, post-peak physical residency, fragmentation and
  GPU core cache/event/FD behavior. Pool capacity and RSS alone do not prove this.
- Thermal/power drift and energy per useful delivered result.
- Repeated no-loss validation of the external C++/host-staging comparisons,
  real VLN/rosbag, Orin, and the research 10% acceptance requirement.
- A DDS message-reuse/preallocation control, alongside the current fresh-object
  baseline, to separate allocation policy from transport effects.
- End-to-end direct-production feasibility: the real sensor/model operator must
  produce into the borrowed shared storage. Copying a pre-existing CPU/GPU tensor
  into it would not establish the intended payload zero-copy pipeline.
- Startup/recovery availability and mixed payload sizes in one live process.
  Per-size fresh-process latency does not measure service downtime or mixed-size
  memory efficiency; existing crash safety checks do not replace those metrics.

CPU reclaim policy A/B is deferred; size-policy/dynamic-allocation changes remain
separate from GPU metadata optimization. No global SHM cleanup, CUDA reset, force unload
or allocator reset was used in these checks.
