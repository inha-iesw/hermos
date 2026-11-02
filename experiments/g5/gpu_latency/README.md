# G5 bounded GPU latency

This scenario measures the adopted G4 Python GPU path in separate registered
publisher/subscriber processes on Thor. It uses the existing launcher, private
epoch and canonical repaired CUDA core. It does not change their lifecycle or
public API contracts.

## Measurement contract

- Five dense uint8 tensors: 1 KiB, 4 KiB, 64 KiB, 1 MiB and 4 MiB. Unlike the
  CPU scenario, these sizes **include** the eight-byte sequence header.
- 16 producer blocks, at most 16 consumer reads, 50 Hz, 64 warm-up publications
  excluded, then 1,000 measured sends per size. Smoke uses 16 measured sends.
  The nominal block reuse interval is 320 ms, above the adopted 100 ms grace.
  This is paced bounded load, not saturation or long-duration stress.
- Fill and sequence header are written directly on the GPU in borrowed storage.
  Writable views are deleted before publish. The consumer reads the whole
  tensor using an int64 checksum on its explicit bound stream, records an event
  and waits for completion. It then checks the sequence and checksum with two
  scalar DtoH transfers: 16 diagnostic bytes per publication, after the timed
  GPU completion. This is a checksum oracle, not byte-by-byte golden equality.
- Host CLOCK_MONOTONIC boundaries are `publish_to_callback_ns`,
  `publish_to_gpu_done_ns`, `fill_to_gpu_done_ns` and `publish_call_ns`.
  GPU-done metrics include GPU mapping/import, checksum work and the host event
  wait. The callback boundary includes descriptor dispatch and lightweight lease
  construction; mapping starts inside the callback's `as_tensor()` call.
- Raw send/receive timestamps are joined by sequence after the run. Duplicate,
  unsent or malformed records fail; receipt loss remains explicit and exits 2.
  The full matrix continues after loss, including zero receipts (N/A latency),
  and exits 2 after saving all cases. Invalid evidence still fails immediately.
  Both peer time namespaces must match.
- Statistics retain count, mean, min, max, population variance, p50 and p99.
  Process RSS/HWM/FD snapshots and wrapper owner counters are observations.
  End-of-run active/pending owners must be zero. The native `wrapper_events`
  field aliases active owners; it is not an independent CUDA event counter.
  all peers and adopted descendants must terminate successfully.

Configured pool capacity is 16 KiB through 64 MiB. It is not physical VRAM:
allocation granularity, CUDA/Torch caches and mappings are outside that figure.
Zero wrapper owners and stable sampled FDs do not prove allocator leak
freedom or bounded core event/cache high-water. The latest larger-payload run
does not repeat the G4 nsys copy audit.

## Build and run

From the workspace root, after the CPU production build:

```bash
bash experiments/g5/cpu_latency/production.sh
bash experiments/g5/gpu_latency/production.sh
bash experiments/g5/gpu_latency/run.sh "$PWD/log/g5/gpu-smoke-new" --smoke
bash experiments/g5/gpu_latency/run.sh "$PWD/log/g5/gpu-new"
bash experiments/g5/gpu_latency/verify.sh
bash experiments/g5/gpu_latency/run.sh "$PWD/log/g5/gpu-compare-smoke-new" --smoke --compare-dds
bash experiments/g5/gpu_latency/run.sh "$PWD/log/g5/gpu-compare-new" --compare-dds
```

Use fresh stage paths and run sequentially with other device/DDS workloads.
The corrected core/messages install goes to `install/hermos/g5/gpu-comparison-core`;
the G4 adapter goes to `install/hermos/g5/gpu-comparison`. Both use
Release/BUILD_TESTING=OFF and copy installs over the CPU production prefix.
The core stays a static archive while ROS messages retain shared Fast DDS
typesupport. The earlier `gpu-core`/`gpu` installs and their results are preserved.
`verify.sh` explicitly requires the GPU installs; it checks the actual runtime
with fake native ownership and creates real endpoints under both RMWs without
CUDA work. Pure matrix tests also run in the default `scripts/verify`; set
`G5_VERIFY_GPU_HOST=1` to include this explicit host gate in that command. Existing G2/G3/G4 evidence directories remain intact.
`prepare.py` verifies the existing prepared tree against the locked upstream
and canonical lifecycle patch; it does not silently repair a modified tree.

The runner saves protocol, source hashes/archive and installed artifact hashes
before measurement, checks actual project library paths, and requires unchanged
inputs at completion. Sources are scenario-local Python namespace modules,
invoked with `python -m` by `run.sh`. The existing G4 launcher owns runtime
registration, execution, signal handling and drain. Benchmark code retains the
caller-owned stream/event through that drain. No general ROS launch support or
automatic bridge is introduced.

## Results and remaining comparisons

The initial full result is `log/g5/gpu-01/results/summary.json`: 5,000/5,000
measured receipts, with successful checksum and wrapper-drain checks.
The matched full comparison is `log/g5/gpu-compare-01`: 20,000/20,000 measured
receipts across both RMWs and transports. HERMOS mean publish→GPU-done latency
was higher in all ten matched pairs (+79.83–281.97% in this single bounded run).
See [G5 evidence](../EVIDENCE.md) for boundaries and provenance limitations.

The initial five-case run has no matched DDS descriptor baseline. The new
`--compare-dds` mode runs 20 matched conditions: five sizes × both RMWs × DDS and
HERMOS descriptors, shuffled with seed 20261101. The default five-case mode
retains ascending size order. Both use the same core, pool, TensorLoan/Lease,
Torch operations and explicit streams. `dds_endpoint.py` supplies benchmark-only
stock DDS endpoints with the runtime's endpoint ownership/drain contract; the
product facade is unchanged. This comparison includes metadata API, serialization
and heap integration costs, and is not a transport-only comparison.

Reports retain publish-call time, callback and GPU-done boundaries, and
fill→GPU-done mean/p99. A callback can precede the publisher's return, so these
intervals overlap and cannot be added or subtracted to derive pure transit cost.
Positive comparison percentages mean additional latency: `(HERMOS / DDS - 1)`.
Prepare, metadata writing and descriptor publish costs inside `publish()` still
need independent instrumentation before optimization. Legacy upstream examples
use a different core and cannot replace this matched comparison. Real VLN/rosbag,
Orin, sustained resource/copy observability and GPU bridge remain unassessed.
