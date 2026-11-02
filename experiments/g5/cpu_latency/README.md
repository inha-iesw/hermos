# G5 synthetic CPU latency

This scenario compares stock rclpy DDS and the typed HERMOS Node facade using
the same generated ROS messages, separate publisher/subscriber processes, and
the same QoS. It is the first G5 measurement, not the real VLN/rosbag evaluation
or a completed `SPR.ZCP.001` requirement.

## Fixed measurement contract

- Payload sizes: 1 KiB, 4 KiB, 64 KiB, 1 MiB and a configured limit of 4 MiB.
  Every type also contains an eight-byte sequence number. Size means payload
  bytes, not serialized size or native allocation capacity.
- Each size has a fixed-array and an unbounded-sequence case. Both transports
  use the exact same ROS message class for each case.
- Cyclone DDS and Fast DDS are each run with stock DDS and HERMOS: 40 cases.
  Their order is shuffled with seed `20261101` before any result is observed.
- QoS is KEEP_LAST, depth 4, BEST_EFFORT, VOLATILE. Publication is paced at
  100 Hz. Full runs exclude 100 warm-up messages and retain 1,000 measured sends.
  A smoke run also warms up 100 messages, then checks 16 measured messages.
- Publisher records local CLOCK_MONOTONIC immediately before `publish()`;
  subscriber records it as its callback's first statement. Sequence matching
  happens after both processes save their records. There is no parent ACK or
  log output per timed message and no timestamp setter in the message.
- `publish_to_callback_ns` includes the transport, DDS deserialization or
  HERMOS Waitable/proxy dispatch up to Python callback entry. The separate
  `fill_to_consumer_ns` begins before allocation/fill and ends after callback
  content checking. `publish_call_ns` is a third, distinct boundary.
- Timing runs validate length and first/last bytes. Smoke runs check the entire
  payload. HERMOS fills shared storage directly. DDS creates an owning ROS
  message and fills its local storage; serialization occurs in stock publish.
- Raw samples, expected/received/lost counts, mean/min/max/population variance,
  p50/p99 and the observed delivery rate are retained. Missing receipts are
  reported; the remaining matrix still runs, but incomplete comparisons are
  invalid and the command exits 2 after saving all results. Protocol or process
  failures stop immediately. Favorable incomplete cases cannot pass.
  Zero measured receipts retain publish-call statistics and N/A receipt latency.
  Delivery rate is `(N - 1)` divided by the first-to-last measured callback
  span; fewer than two receipts gives N/A. This is not maximum throughput.
  Warm-up snapshots use the first received sequence at or beyond the boundary
  and record that sequence, so a lost boundary message does not omit observation.
- Reports show matched-RMW mean/p99 changes for both publish→callback and
  fill→consumer, plus publish-call cost. Positive percentages mean additional
  latency: `(HERMOS / DDS - 1) × 100`; negative values mean lower latency.
  They do not certify a 10% research improvement. That requires the agreed
  real-workload comparison, and the current dataset/pipeline is not supplied.
- RSS, high-water RSS, virtual memory and FD counters are process snapshots.
  They are not allocator leak/fragmentation proof, physical memory accounting,
  or evidence that the 1,000-iteration leak criterion has passed.

Baseline-B is not represented by relabeling Python DDS. Humble rclpy has no
loaned-message path for the proposed native C++ zero-copy comparison; a native
C++ benchmark would be a separately identified experiment.

## Build and run

From the workspace root:

```bash
bash experiments/g5/cpu_latency/production.sh
bash experiments/g5/cpu_latency/run.sh "$PWD/log/g5/smoke-new" --smoke
bash experiments/g5/cpu_latency/run.sh "$PWD/log/g5/cpu-new"
bash experiments/g5/cpu_latency/run.sh "$PWD/log/g5/rate-smoke-new" --smoke --rate-diagnostic
bash experiments/g5/cpu_latency/run.sh "$PWD/log/g5/rate-new" --rate-diagnostic
```

Each stage must be new. The build uses `build/hermos/g5/production` and
`install/hermos/g5/production`, Release/BUILD_TESTING=OFF, a copy install, and a separate
Cargo target with no `test-observation` feature. It does not replace the G2
verified kernel module/artifact directory. The runner checks the build options,
heaphook feature/symbol provenance, installed hashes and actual peer mappings.
It writes `protocol.json` and `provenance.json` before the first measurement.
Source/artifact identity must remain unchanged through the run. Measured source
files are also archived, since the working tree may contain uncommitted changes.

The canonical device session runner verifies and loads only its owned module,
then waits for zero references and unloads it normally. Run sequentially with
other device/DDS tests. Each case uses a new private IPC/mount namespace and
private `/dev/shm`, via the existing root-owned bootstrap. The supervisor is not
preloaded. Only HERMOS peers preload the new production heaphook, beside the
matching library. Bridge and discovery remain disabled for all peers.

This adds a narrowly scoped **benchmark-only CPU registered peer**. It creates
endpoints before atomically registering an empty GPU-resource inventory, requires
both registration environment variables and ACK, then starts dispatch. It does
not implement a general CPU launcher or change the GPU runtime's registration
contract. Both peers save results before group TERM; each explicitly closes the
executor/Node/context. Peer exit codes, worker death, ECHILD and natural unlink
daemon exits must all succeed. No daemon kill, driver SHM deletion, force unload,
old-stage adoption or live-pool reset is permitted.

The new Python files are scenario-local; they are run with `python -m` from the
workspace by `run.sh`, not installed as a product API. The two colcon packages
under `../messages` and `../adapters` own ROS type generation and native provider
discovery. Host tests are registered by `g5_benchmark_adapters` and included in
`scripts/verify`; they can also be run directly:

```bash
python3 -m pytest experiments/g5/cpu_latency/test -q
```

## Rate-only DDS diagnosis

`--rate-diagnostic` is a separate four-case experiment: dynamic 1 MiB under each
RMW at 100 Hz and 20 Hz, keeping QoS depth 4/BEST_EFFORT and default profiles
fixed. It retains 100 warm-up + 1,000 measured sends (smoke: 16) and adds process
CPU/context-switch/owned UDP socket snapshots plus network-namespace UDP counter
deltas. Namespace counters include unrelated traffic and warm-up/teardown;
they locate observations but do not prove packet-level cause. No sysctl or DDS
profile is changed, and diagnostic results never replace the original baseline.

The three scenarios share unmeasured control/evidence operations in
`../harness.py` and clean overlay setup in `../environment.sh`. Payload fill,
publish and callback timestamp statements remain in their scenario peers.

## Remaining G5 scope

The [bounded GPU measurement](../gpu_latency/README.md) now covers five sizes.
Sustained-load/leak observability and native GPU IPC comparison,
automatic bridge lifecycle design, the actual
VLN/rosbag pipeline, Orin validation and final requirement reports remain
separate work. An explicit CPU relay is measured as its own scenario, with
materialization costs included. Current GPU group recovery is not proof of uninterrupted VLN
pipeline operation. Current GPU READWRITE mapping is not CPU heap read-only
protection.
