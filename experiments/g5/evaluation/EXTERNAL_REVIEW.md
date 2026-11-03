# Latest external benchmark evaluation

Reviewed 2026-11-03 KST. Source: user-provided
`.cache/HERM0S 전송 벤치마크.html`, SHA-256
`140ec8a8daebd9608077b262c03d09e9cf51dc762944baed50ee0d2c3a0e28ef`.
The HTML identifies its measurements as 2026-11-02. Its JavaScript was not executed;
the embedded `const DATA` JSON and local raw evidence were read independently.
The user identified <https://claude.ai/artifact/4rBppowXZTig2UDZh9eVuL> as the latest
published version; HTTP access was blocked, then the local HTML supplied its text.

## What the new evidence establishes

Raw results and peer code were located under
`/tmp/claude-2001/-workspace-rclpy-zerocopy-ws/892d4508-51bf-4c96-a2cd-084ddebcc728/scratchpad/`:

| Experiment | Raw results | Scope |
|---|---|---|
| Fanout | `fanout/stage-full-01/results` | 80 CPU/GPU, Python/C++, size, subscriber-count and reclaim conditions |
| GPU memory follow-up | `fanout/stage-gpumem-01/results` | 32 conditions, 150 messages, separate telemetry run |
| GPU 1:1 | `gpu4/stage-full-01/results` | 16 conditions, pageable host-staging baseline |
| CPU reclaim A/B | `zc_ab/stage-full-01/results` | 24 conditions, separate eager/no-reclaim builds |

HTML `DATA` equals the scratch `viz/data.json`. All 80 fanout received vectors and
720 displayed numeric values match the raw summaries within display rounding.
Independent timestamp recomputation also matches all 80 subscriber-p50 medians
and all 80 sets of stage medians. All 80 peer clock namespaces match and all 80
teardowns have the required flags and exact integer-zero exits. This is useful
evidence of data consistency and cleanup, with the qualifications below.

The [audit JSON](../../../log/hermos/g5/external-review-01/audit.json) retains
462 input hashes. The reviewed [HTML](../../../log/hermos/g5/external-review-01/input.html)
and [raw JSON archive](../../../log/hermos/g5/external-review-01/raw-evidence.tar.gz)
are preserved in the workspace, with
[member hashes](../../../log/hermos/g5/external-review-01/preserved-inputs.json)
and [reproduction instructions](README.md#external-html-audit). The archive
preserves available evidence; it does not reconstruct the missing historical
fanout source files. This audit does not rerun payload checksums.
The currently inspected `fanout_driver.py`, `gpu_mem_driver.py` and `extract.py`
are separately retained in `observed-sources.tar.gz`, with timestamps/hashes in
`observed-source-hashes.json`; they are review snapshots, not reconstructed
historical execution inputs.

The baseline questions differ:

| Comparison | GPU payload path | Question answered |
|---|---|---|
| External HTML vanilla vs HERM0S | Pageable D2H → DDS → H2D versus CUDA IPC | Benefit of the selected end-to-end copy-removal implementation |
| Existing/new G5 same-core comparison | CUDA IPC in both paths; DDS versus HERM0S descriptor | Descriptor API, synchronization and transport overhead |

Both comparisons are useful. Their speed ratios measure different changes.
C++ vanilla/HERM0S use the same custom checksum kernel, while Python uses
`torch.sum`; cross-language ratios therefore include consumer implementation costs.

## Corrections supported by raw data

1. **Stage medians are not additive E2E means.** HTML line 211 says each stage is
   the median of subscriber means, then calls their sum E2E mean. In 32/80 cases,
   that sum differs from the raw median of subscriber E2E means by more than
   0.002 ms. For Python vanilla GPU, 4K/N4, the displayed sum is **44.876 ms**;
   the actual median of subscriber means is **44.440297 ms**, their equally
   weighted mean **44.344104 ms**, and pooled receipt mean **44.383078 ms**.
   Use one consistent arithmetic weighting for additive stages, or label the
   sum as a constructed stage total and report the actual E2E statistic separately.

2. **The HERM0S case denominator is wrong.** The HTML's 39/40 no-loss claim should
   be **31/32 for default reclaim**, or **47/48 including reclaim OFF**. Vanilla
   has 32 conditions and all 32 have some receipt loss. Fanout exit 0 means no
   exception/teardown failure; the driver does not fail its exit status on loss.

3. **Lossy survivor latency is not matched-delivery performance acceptance.**
   The 1:1 GPU C++ 4 MiB “2.3×” compares 1,000/1,000 HERM0S receipts against
   959/1,000 vanilla receipts. CPU static 4 MiB “19×” compares 1,000 against 966.
   The displayed p50 ratios are arithmetic ratios of received observations; the
   missing latencies are unknown. Preserve the ratios with that label, and reject
   the paired acceptance verdict until a no-loss load is measured for both.
   The blanket CPU reclaim-OFF “2–3×” wording also exceeds its own Python 4K
   N2/N4/N8 ratios: **1.43× / 1.65× / 1.82×**, before considering loss.

4. **Page faults do not explain the entire consumer interval.** In 4K/N1 CPU,
   reclaim OFF still consumes **12.209 ms (C++) / 24.824 ms (Python)**, compared
   with ON **16.710 / 29.086 ms**. The observed differences are **4.501 / 4.262 ms**.
   The A/B supports a reclaim cost, but not attribution of the entire ON interval
   to faults. Likewise GPU-idle, fragment loss and queue-depth explanations need
   direct counters or controlled experiments; size correlation alone is insufficient.

5. **The GPU payload model equals vanilla at N=7.** Its equations are eight pool
   blocks versus N+1 vanilla buffers. It is larger through N=6 and equal at N=7;
   N=7 was not measured (N=1/2/4/8 were). The statement must also distinguish
   payload allocation from total GPU memory. In Python/1080p, total GPU telemetry
   already favors HERM0S at N=2: **694 versus 706 MiB**. The model's workspace
   term includes observed Torch reserved allocations, which differ between
   variants in that condition; it cannot always be removed identically. Reserved
   bytes include allocator caching/padding, not only active reduction workspace.
   N=7 equality is a property of the displayed allocation model. The raw records
   omit the VMM allocation granularity, so physical-byte equality is not established.

6. **The process-count methods paragraph needs a scope label.** Its “two
   processes for all cases” statement applies to 1:1; fanout uses N+1 peers.

## A different view: useful delivery to every subscriber

The displayed median of subscriber p50 values is correctly calculated. It does
not describe the time when every subscriber has finished the same message.
The audit therefore joins IDs across all subscribers and uses the last completion
timestamp. Missing IDs remain unsuccessful deliveries, not invented latencies.

| Condition | Aggregate receipt rate | Worst subscriber receipt rate | IDs completed by every subscriber | All-subscriber completion p50 |
|---|---:|---:|---:|---:|
| HERM0S CPU Python, 1080p/N8 | 94.83% | 58.67% | 176/300 (58.67%) | See machine-readable audit |
| HERM0S GPU C++, 4K/N8 | 100% | 100% | 300/300 | **12.374 ms**, versus displayed 7.301 ms |
| HERM0S GPU Python, 4K/N8 | 100% | 100% | 300/300 | **21.062 ms**, versus displayed 14.503 ms |
| Vanilla GPU Python, 4K/N8 | 63.79% | See audit | **24/300 (8%)** | **60.466 ms**, conditional on those 24 IDs |

The HERM0S CPU outlier has received counts
`[300,300,300,300,300,300,176,300]`: aggregate loss 5.17% hides one subscriber's
**41.33% loss**. For a pipeline requiring every branch, the all-subscriber metric
is the relevant additional view; independent subscribers may require a different
application contract.

The [delivery/completion figure](../../../log/hermos/g5/external-review-01/delivery-completion.png)
and [PDF](../../../log/hermos/g5/external-review-01/delivery-completion.pdf)
show these selected eight-subscriber conditions without combining their workloads.

Both HERM0S 4K/N8 GPU cases finish all 300 IDs within the offered 33.333 ms period.
Vanilla Python finishes none within that budget. The offered period is a
diagnostic threshold, not an agreed VLN deadline. Vanilla's reported 26.1106 Hz
also hides stalls/catch-up bursts: fill-start gaps have p50 **24.706 ms**, p99
**121.828 ms**, maximum **127.211 ms**, with **1.4874 s** cumulative schedule drift.

## Memory: observations, models and causality

- Label the host metric **sum of observed-process PSS increases**. PSS apportions
  resident mappings; it does not include every kernel, unmapped tmpfs, external
  sharer or device allocation. It is not system-wide physical DRAM usage.
  See the [Linux proc documentation](https://docs.kernel.org/filesystems/proc.html).
- The fanout collector labels `RssAnon` as private memory. Anonymous resident
  bytes and private mappings are different categories; the raw smaps records also
  contain actual `Private_Clean`/`Private_Dirty`. For GPU C++ HERM0S 4K/N4,
  publisher RssAnon grows 0.1172 MiB while private mappings decrease 0.09375 MiB.
  Use the corresponding smaps categories instead of equating these counters.
- The 1:1 system-footprint estimate subtracts each run's warm-up MemAvailable
  from a global median teardown baseline. Its stated ±30 MiB accuracy is not
  established: the 16 available teardown baselines have sample SD **121.39 MiB**
  and maximum deviation from their median **480.78 MiB**. This is a diagnostic
  of background/baseline variation, not a calibrated error bar for each result.
- The host+GPU chart combines host PSS deltas from the 300-message latency run
  with modeled GPU payload allocation tied to the separate 150-message run.
  It is a hybrid estimate, not a simultaneous total-memory measurement. For its
  C++ 4K configuration, N1 **143.3 versus 192.5 MiB**, N2 **214.9 versus 192.9 MiB**,
  and N8 **644.6 versus 195.1 MiB** are internally consistent estimates, not a
  universal crossover result.
- Per-process `nvidia-smi` sums need an allocation/import/touch control to establish
  how CUDA IPC mappings are attributed on this driver and avoid double counting.
  The [NVIDIA SMI documentation](https://docs.nvidia.com/deploy/nvidia-smi/) defines
  reported accounting, rather than proving unique physical-byte attribution for
  this experiment. The “context” component is a fitted residual after subtracting
  modeled payload and the allocation/workspace terms; constant residuals do not
  independently identify it. The GPU-memory run has four or five complete timestamp
  samples per condition. Although its driver sums per-peer medians, using median
  simultaneous totals gives the same value in all 32 cases, so that aggregation
  choice does not alter this particular dataset.
- Do not reconstruct signed PSS deltas as `private + max(PSS-private, 0)` and call
  the result measured PSS. That changes, for example, a GPU C++ HERM0S N4 total
  from **1.5 to 1.7 MiB**, and N8 **3.1 to 3.3 MiB**. Aggregate unrounded values
  first, then round only for display. “About 3 MiB” also fits the 3.1 MiB maximum
  better than “at most 3 MiB”.
- Three snapshots cannot establish continuous post-warm-up stability or leak
  freedom. The available result is no additional growth observed at those sampled
  points. Long reuse, peak-after-release, mixed sizes, retained aliases and multiple
  publishers remain necessary. Reducing pool blocks requires a concurrency/borrow
  failure study, not only multiplication of a smaller block count.
- The declared 4 and 7+ payload copies are code-path estimates, explicitly labeled
  as such in the HTML. They are not measured memcpy counts or DRAM traffic.
  Payload copied-byte volume excludes read/write traffic, caches, consumer reads
  and reduction workspace. Zero staging copies still permits O(N × payload)
  consumer reads even when payload allocation remains O(1).

## Reproducibility and remaining validation

Fanout uses a fixed scenario→N→size→variant order and one run per condition.
Each subscriber has 170–300 received observations, so p99 depends on roughly two
or three extreme points. Neither subscriber count nor message count substitutes
for independently repeated runs. Repeat and alternate paired variants, report
every loss, and evaluate a no-loss load plus a separate overload curve.

The fanout provenance records CPU artifacts and C++ executable hashes but omits
GPU overlay artifact hashes, the executed C++ source, the modified no-reclaim
source, a source archive, and end-of-run identity verification. Its Python GPU
peer also changed between latency and memory runs:

- latency hash: `57e195f6e06d6857abaaca456675abbb802b509e9da6e2bf693bb5eaf751aa60`
- memory/current hash: `a8c7a2d5ef251300b272ef0382792ff8f83a6ed1e4eb1d8ce81c36452001cff4`

Without the earlier file contents, their code equivalence cannot be established.
The 1:1 GPU and reclaim runs have source archives and retain their loss exit 2;
the fanout exit 0 should not be substituted for that stricter acceptance contract.

The reported GPU integration 31 PASS and G3 namespace **4 CTest targets** PASS
have corresponding workspace logs. The CPU signal gate retains **2 failures,
9 passes and 2 fixture errors** in
`log/hermos/device-arm64-validation-04-lifecycle/`. Read-only tracing identifies
a stock Humble WaitSet/context-shutdown race escaping the test peer's
`ExternalShutdownException` handler. Signal handling itself completed; the peer
emitted `SIGNAL context_shutdown=true` after `ERROR`. In those failed runs, the
relevant peer code was unchanged apart from rename; Node destruction followed
executor thread join.
This supported a test-peer exception-classification gap, not a demonstrated
HERM0S memory/owner defect. A subsequent controlled reproduction and narrow
test-peer correction passed ten host regressions and 26 typed device tests in
`log/hermos/cpu-lifecycle-fixed-01/`; see the
[follow-up evidence](EVIDENCE.md#cpu-signal-shutdown-follow-up). The original
failed logs remain unchanged. Host opt-in skips and GPU PASS were not used to
resolve the CPU signal failure.

The subsequent [repeated Python GPU fanout](EVIDENCE.md#repeated-python-gpu-fanout)
adds three alternating repeats with explicit all-subscriber and worst-subscriber
metrics, full source/artifact identity and a loss-sensitive exit status. Its 4K
DDS baseline loses receipts even at 5 Hz, so a workload-preserving no-loss 4K
comparison remains unestablished. Further rate/transport diagnostics,
allocation-only→mapped→touched→full-consume memory controls and a common
consumer-kernel control across languages remain separate work. The specific CPU
signal failure has the targeted follow-up above.
Further reclaim policy work and reclaim-specific experiments are deferred at the
user's request (2026-11-03 KST); the historical findings above are retained.
Real VLN/rosbag and Orin are still separate requirements.
