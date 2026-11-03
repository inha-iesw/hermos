# Repeated GPU Python fanout

This scenario compares pageable GPU→host→stock rclpy DDS→GPU staging with HERM0S
CUDA IPC for one publisher and eight separate subscribers. It uses the existing
registered private epoch, normal owned-resource teardown and unchanged reclaim
policy. The HERM0S peer is the production G5 GPU factory; the stock peer uses the
same ROS timer pacing and whole-uint8 GPU checksum. Both record completion before
the same two scalar diagnostic reads (16 DtoH bytes per received message).

The default matrix is 4K RGB (24,883,200 bytes), Fast DDS, BEST_EFFORT depth 4,
eight pool blocks, 64 warm-up plus 300 measured messages, 5 and 30 Hz, and three
adjacent alternating transport repeats. The pool/read budgets remain fixed.
The 5 Hz setting is a **no-loss candidate**, not a promised outcome. Both rates
retain every missing receipt and actual sender cadence. P99 with 300 observations
is exploratory; messages and subscribers are not independent experiment repeats.

After the existing GPU production build and device bundle are ready:

```bash
taskset -c 0-13 bash experiments/g5/gpu_fanout/run.sh \
  "$PWD/log/hermos/g5/gpu-fanout-01"
```

`--subscribers 1 8` adds a one-subscriber control. `--rates 5`, `--repeats 1`,
`--warmup 4 --samples 8` can select a separately labeled smoke run. The output
must be fresh and absolute. Do not edit measured sources or rebuild artifacts
during a run: source archives and start/end identity use the existing GPU gate.

`--size 65536 --rates 5` selects a separate 64 KiB, 5 Hz control run; default size
remains 4K RGB. Lowering rate alone need not eliminate large-message loss. The
small-message control changes payload size and must not be presented as a pure
rate effect. Its delivery outcome is still measured rather than assumed.

Each case preserves config, publisher/subscriber-N raw timestamps and counters,
all-receiver drain counts, teardown and recomputed analysis. The report includes
per-subscriber receipts, worst-subscriber loss, common message IDs, the last
subscriber's completion for each common ID, loss bursts, sender drift and the
offered-period diagnostic. It never treats missing messages as zero latency.
All-reader latency in a lossy condition is conditional on common receipts; loss
invalidates paired performance acceptance. Missing receipts cause final exit 2
after all conditions; protocol, identity and cleanup errors remain failures.
Drain starts COUNT rounds for up to five seconds, with a separate one-second
response deadline shared by all readers in each round. No new round starts after
the payload deadline; a round already sent may finish, giving a maximum budget
of 5 + 1 seconds apart from scheduling overhead. A reader that does not answer
within that response budget remains a protocol error. This avoids treating a
normal 5 ms control reply near the payload deadline as a failure.

Peers save sequentially after drain, so the five-second drain is not a common
receipt cutoff. Later peers may receive additional messages before saving;
final raw records, rather than the last COUNT response, determine loss. Warm-up
counts publisher sends, not guaranteed receipts at every subscriber. A complete
measured interval does not imply that every reader received every warm-up ID.

A valid paired effect requires complete measured receipts, but does not itself
assert equal achieved load. Inspect actual sender Hz, gaps and drift alongside
latency. Completion starts at the actual fill call, excluding any delay before
that timer callback begins. The two scalar reads and validation occur after each
completion timestamp but still affect subsequent processing, cadence and loss.
These results describe that full consumer workload.

This is a payload-path comparison, separate from `gpu_latency --compare-dds`,
where both transports use the same CUDA IPC payload path. It does not isolate
transport from allocation, pageable staging or language conversion overhead.
The checksum implementation and dtype are matched within this Python comparison.
No fixed-clock, saturation-throughput, physical-memory, real-VLN or Orin claim
follows from this bounded scenario.

Host regression tests require no GPU initialization:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest experiments/g5/gpu_fanout/test -q
```
