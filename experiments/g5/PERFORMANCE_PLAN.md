# HERMOS G5 performance improvement plan

Updated: 2026-11-03 (Asia/Seoul).

This is an implementation and validation plan, not a performance result.
The HERMOS package rename is separate from the optimizations below. Neither
renaming packages nor passing host tests establishes improved device performance.

## Evidence and measurement boundaries

The retained baseline is [EVIDENCE.md](EVIDENCE.md), especially
`log/g5/gpu-compare-01` and `log/g5/cpu-01`. Those records use the former
Agnocast names and must not be rewritten as measurements of renamed or optimized
binaries. The 2026-11-02 performance review and its scratch experiments provide
additional mechanism evidence, not an optimized Thor run.

- GPU: both comparison paths use the same CUDA IPC payload storage. The
  descriptor API/transport differs. The original rank-one descriptor requires
  138 scalar worker writes, three resizes, one borrow and one publish: 143
  synchronous worker commands. Reproducing a similar pattern with TestMessage
  supports worker synchronization as the main publisher cost. It does not replace
  measurement using the actual generated GpuTensor provider.
- A single worker operation measured at about 0.024 ms is a lower bound for the
  handoff, not the latency of constructing and publishing a complete descriptor.
  Approximately 0.3 ms publish latency after optimization is a hypothesis.
- CPU: the fork's synchronous MADV_REMOVE on freed shared-heap ranges is followed
  by faults when those pages are used again. A paced tmpfs control reproduced
  much of the observed size-dependent cost, but the actual heaphook eager/retain
  device A/B has not run. The control's 4 MiB fill near 0.15 ms is not a product
  result or acceptance threshold.
- publish-call, publish-to-callback and publish-to-GPU-done overlap. Do not add
  them. GPU-done includes mapping/import, checksum work and a completion wait.
  CPU fill-to-consumer includes borrow/allocation/fill and the chosen consumer
  work; full-payload consumption must be distinguished from endpoint checks.

## P0: establish comparable measurements

Record the exact sources, loaded artifacts and selected memory policy. Capture
publisher/subscriber CPU affinity, governor and relevant frequency/power state;
use the same scheduling policy for paired runs. A one-core affinity diagnostic
must be reported separately from deployment-like runs. The parent runner's
allowed affinity alone does not describe worker placement.

Add publisher and subscriber minor/major page faults and per-message context
switch/CPU-time deltas to the existing observations. Use tmpfs usage and shared
mapping residency/PSS as well as RSS; summing peer RSS double-counts shared pages.
Keep detailed timing/trace instrumentation in separate diagnostic runs so it
does not change the main comparison.

Retain the original GPU 50 Hz and CPU 100 Hz baselines, warm-up and message sizes.
Use at least three alternating baseline/variant repeats with the same protocol.
Report mean, median, p99, loss, CPU cost and memory observations. Missing messages
invalidate the paired latency comparison; do not silently drop failed cases.

## P1: GPU caller-thread writes and traversal reduction

Use the existing native owner.write for already allocated scalar fields instead
of _write_on_worker. Keep borrow, structural resize, publish, cancel and native
object lifetime operations on the publisher worker. Preserve creator-thread,
generation, type/range, terminal-state and writable-export checks.

Finish all string/sequence resizes before the first write or buffer export and
use the current structural generation afterward. Avoid visiting primitive leaves
in the resize pass and avoid redundant snapshot/info calls. Retain schema and
fixed-array length validation. Do not assume arbitrary nested sequences contain
only scalars: optimization is scoped to the known GPU descriptor schema.

For the original descriptor layout, the expected structural command count falls
from 143 to five. Verify the command pattern separately from timing; a particular
context-switch count is an observation, not a scheduler-independent guarantee.

Primary implementation boundary: GPU endpoint metadata handling and its tests.
Update documentation that currently requires all descriptor writes to run on the
worker. Removing an unused private native helper is not required for this change.

## P2: GPU bulk metadata access

Use scoped native buffer views for the event handle and UTF-8 socket path, then
release every view before publish. Use equivalent bulk reads on reception.
Preserve exact fixed lengths and current strict scalar validation, including bool
versus integer distinctions; byte conversion must not silently widen the API.

After P1/P2, measure prepare, descriptor borrow/resize/fill, CPU publish and
receiver decode/map separately. Only consider a native batch API if material
cost remains. A batch executed on the typed worker can be safe; moving private
C++ string/vector storage into a shared message or growing it on the wrong
allocation thread is not safe. Do not bypass owner checks for a faster result.

The decode control explained about 70 microseconds of the observed receiver gap,
not its entirety. Attribute any remaining difference only after device timing.

## P3: CPU heaphook eager/retain A/B

Keep logical message destruction, reference release and TLSF deallocation in
both modes. Compare current eager page return against an explicitly selected
retain mode that omits MADV_REMOVE and permits immediate allocator reuse.
Select the policy once during safe initialization, not by allocating or querying
the environment in every free/malloc hook. Record it in artifact provenance.

Preserve the existing default until device results and memory observations justify
a deployment choice. Retain mode trades prompt post-peak tmpfs shrink for reuse;
it is not evidence of a leak, nor a promise of bounded physical memory. This
policy is independent of per-message byte-limit removal and GPU pool growth.

Test large/small/large sizes, repeated reuse, retained aliases, and multiple live
publishers. Run both endpoint-check and separately labeled full-consume trials
under identical conditions for both transports. Confirm all data before using
latency results and measure publisher/subscriber page faults after warm-up.

If a free-page budget is needed later, design it around actual allocator free
ranges and split/coalesce/reuse behavior. Never queue a raw freed address and
punch it later without revalidation: it may already contain live data. Moving
reclaim out of publish alone does not eliminate the next fill's faults.

Keep allocator metadata guards and lock ordering. Check both adjacent live
blocks and the platform's actual page size; cover 4/16/64 KiB range arithmetic
before claiming portable reclaim behavior. No live-pool reset or global SHM
cleanup is part of this optimization.

## Acceptance and order

1. Document and instrument the baseline without mixing diagnostic timings into
   performance results.
2. Apply P1; verify actual native field values, invalid input, stale generations,
   foreign-thread rejection and alias-held publish rejection.
3. Apply P2; additionally verify buffer release on success/error and the existing
   rule that a descriptor failure after GPU prepare never cancels a committed
   GPU generation early.
4. Reuse Python/Python and Python/C++ device transport, owner drain and teardown
   checks, followed by the matched GPU matrix with fresh result directories.
5. Run actual heaphook P3 A/B independently of GPU changes, including long-run
   reuse and memory behavior; choose deployment policy only from those results.
6. Preserve every old result and publish source/artifact-linked new evidence.
   Real VLN/rosbag, Orin and the research's 10% acceptance requirement remain
   separate outstanding validations.

The implementation boundary is metadata handling, heaphook policy and G5
observations. Kernel migration, a replacement GPU allocator, message-size policy
changes and performance implementation during the package rename are out of scope.
