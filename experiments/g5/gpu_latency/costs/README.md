# GPU call-cost diagnostics

This opt-in peer wraps existing calls without adding CUDA synchronization or
payload reads. Normal `gpu_latency.run` uses the original factory with no cost
observer. Diagnostics are marked `timing_diagnostics=true` and
`headline_eligible=false`; the independent evaluator rejects their speedup verdicts.

From the workspace root, after production artifacts and the device bundle are ready:

```bash
source experiments/g5/environment.sh
source install/hermos/g5/gpu-comparison-core/local_setup.bash
source install/hermos/g5/gpu-comparison/local_setup.bash
taskset -c 0-13 python3 phase4_gpu_core_probe/repair/session/run_device.py \
  "$PWD/log/hermos/g5/gpu-costs-new" \
  python3 -m experiments.g5.gpu_latency.costs.run \
  --output "$PWD/log/hermos/g5/gpu-costs-new/results" --warmup 64 --samples 200
```

The default matrix has 1 KiB and 4 MiB payloads, both RMWs and both descriptor
transports. Every run needs a fresh output directory. Source/artifact identity,
payload checksums, owner drain and teardown use the normal runner's gates.

`publisher-costs.json` and `subscriber-costs.json` retain numeric stage timestamps
and sample IDs alongside the original raw peer records. The recorder retains no
Tensor, loan or returned owner. Stage durations are wall-clock host observations:

- `descriptor_fill_total` includes `descriptor_resize`; their percentiles are not
  additive. The derived remainder includes traversal, validation and instrumentation.
- `tensor_export_total` includes decode and map. Its remainder is derived only
  when those successful intervals are contained and nonoverlapping.
- `map` includes Python/native conversion, owner polling, locks, metadata/FD
  acquisition and CUDA import/map/event work. It is not a memcpy measurement or
  an isolated CUDA API duration.
- `gpu_completion_wait` wraps the existing wait. It can include remaining producer
  work, checksum execution and scheduling; it is not pure checksum kernel time.

The producer's stages and the consumer's stages do not cover all application or
transport overhead. Instrumentation changes timing, so use it to locate costs,
then evaluate any implementation change with an uninstrumented repeated matrix.

Host boundary/delegation/lifetime tests run through
`bash experiments/g5/gpu_latency/verify.sh` with the production GPU overlay.
