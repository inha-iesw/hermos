# G5 independent evaluation

This evaluation reads preserved peer timestamps and teardown records. It does not
rewrite the original run or change a failed/incomplete run into a passing run.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m experiments.g5.evaluation.run \
  --input log/g5/cpu-01/results \
  --input log/g5/gpu-compare-01/results \
  --input log/g5/relay-01/results \
  --output log/hermos/g5/evaluation-legacy-01
```

The output directory must be new. `evaluation.json` contains input SHA-256 hashes,
recomputed distributions, loss bursts, receipt reordering, sender cadence and
drift, completion time relative to the offered period, and process resource
windows. `report.md` presents the main comparisons. The host gate collects
`test/` through the `g5_benchmark_adapters` CMake target `test_g5_evaluation`.

## Boundaries

- Completion starts before borrow/allocation/fill and ends after consumer work.
  The GPU endpoint is synchronized GPU checksum completion; its later scalar
  DtoH diagnostics remain excluded. CPU endpoint checks and full-payload checks
  are different workloads and must retain their protocol labels.
- An offered period is **not an agreed application deadline**. Lost-or-over-period
  counts are a diagnostic of delivery usefulness, not a new acceptance criterion.
- Loss invalidates paired latency effects, including p99. Missing receipts still
  contribute to the lost-or-over-period count. No favorable subset is substituted
  for a complete matrix.
- Receipt and sender rates are paced observations, not saturation throughput.
  Sender drift is relative to the first measured fill start; it cannot recover
  lateness relative to an unrecorded timer origin.
- A 1,000-message run is one run, not 1,000 independent replications. p99 relies
  on roughly ten upper-tail observations. Independent repeat effects remain
  separate; this tool does not manufacture a confidence interval from messages.
- Old resource windows run from warm-up through finish/drain. New P0 windows
  end when the final scheduled ID is observed and retain then-current completeness
  and row counts; late/reordered receipts do not retroactively repair the window.
  CPU/fault/switch deltas use the warm-up **post-collection** counters when present,
  so the warm-up snapshot's own work is excluded. These are process costs, not
  timings of individual functions. The denominator counts every row processed in
  the counter window, including explicitly counted late warm-up receipts; these
  cannot be attributed only to measured messages. Snapshot collection can still
  disturb pacing. Reordered receipts count each sequence below the previously
  received sequence high-water mark, ordered by callback time.
- `--resource-diagnostics` enables detailed memory/thread/frequency collection at
  warm-up and final-ID boundaries. This can exceed the offered period; the
  evaluator labels these runs diagnostic and refuses headline latency effects.
  Normal runs retain detailed before-start/after-drain observations and use light
  counters at measurement boundaries.
- The separate [GPU call-cost diagnostic](../gpu_latency/costs/README.md) sets
  `timing_diagnostics=true`. Its stage timings are also excluded from paired
  performance acceptance, even when delivery and teardown pass.
- Process PSS and shared-heap mapping PSS are distinct. Summed peer RSS double
  counts shared pages. Private `/dev/shm` used bytes cover the entire private
  mount, not only one endpoint. GPU configured pool bytes are not physical VRAM.

## Additional evaluation gaps

The present bounded G5 evidence does not establish sustained-load or post-peak
memory stability, allocator fragmentation, slow-consumer/backpressure behavior
or power efficiency. The external experiments add preliminary fan-out and native
C++ / host-staging comparisons; their repeat, loss and provenance limitations
remain explicit. Full-consume CPU runs, controlled frequency/power observations
and independent alternating repeats are required before attributing a performance
change to one mechanism. Actual VLN/rosbag and Orin remain separate validations.

The [repeated Python GPU fanout scenario](../gpu_fanout/README.md) adds explicit
worst-subscriber and common-ID completion measurements. Current follow-up results
and their acceptance limits are recorded in [EVIDENCE.md](EVIDENCE.md).

The latest Claude artifact was supplied locally as
`.cache/HERM0S 전송 벤치마크.html` after HTTP access was blocked. Its original raw
experiments were also located. See [EXTERNAL_REVIEW.md](EXTERNAL_REVIEW.md) for
the actual comparison, source hash, corrected interpretations and remaining gates.

## External HTML audit

The preserved export, original JSON evidence and archive-member hashes are in
`log/hermos/g5/external-review-01/`. Extract `raw-evidence.tar.gz` into a new
directory, then run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m experiments.g5.evaluation.external \
  --html log/hermos/g5/external-review-01/input.html \
  --fanout-results /path/to/extracted/fanout/stage-full-01/results \
  --output /path/to/new-audit
```

This decodes only the embedded JSON, without executing the HTML or external peer
code. The audit validates timestamps, clock namespaces and teardown; it does not
rerun payload checksum validation or establish missing source-to-binary identity.
It reports worst-subscriber loss, common-ID completion and additive stage means.
In lossy cases each subscriber mean retains that subscriber's own received IDs.
