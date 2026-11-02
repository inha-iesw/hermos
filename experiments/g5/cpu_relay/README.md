# Explicit CPU relay experiment

This scenario measures a three-process source → relay → sink chain using the
existing public CPU endpoints. The relay deliberately copies the full dynamic
payload into its outgoing message. Its sequence number is preserved. No existing
runtime, privileged bootstrap or native bridge behavior is changed.

The fixed matrix has 18 cases: 1 KiB, 64 KiB and 1 MiB, both Cyclone DDS and Fast
DDS, and DDS→DDS, HERMOS→DDS, DDS→HERMOS. The DDS→DDS relay is the matched
control and performs the same explicit fresh-message copy. Each case uses 100
warm-up publications and 1,000 measured publications at 50 Hz, KEEP_LAST depth 4,
BEST_EFFORT and VOLATILE. Smoke mode measures 16 publications after the same warm-up
and enables the existing sink's full-payload correctness check. Cases are shuffled
with seed 20261101 before the first peer starts; results are never reordered to
select a favorable comparison. The protocol must declare dynamic layout and
both transports; unsupported declarations fail before any peer starts.

## Run

Build the production artifacts using the [CPU latency instructions](../cpu_latency/README.md).
Use a new absolute stage for every invocation:

```bash
bash experiments/g5/cpu_relay/run.sh /absolute/new-relay-smoke --smoke
bash experiments/g5/cpu_relay/run.sh /absolute/new-relay-measurement
```

The shell entry point reuses the canonical device runner. All three peers must
register and receive ACK before dispatch inside one fresh private IPC/mount
namespace. `HERMOS_BRIDGE_MODE=off` and `HERMOS_NO_DISCOVERY_AGENT=1` remain
mandatory. Only peers using HERMOS receive its production heaphook preload.
The relay restores TERM handling after endpoint initialization and closes its
executor/node normally. The supervisor owns termination, descendant drain and
verification. No daemon is killed and no global shared-memory entry is deleted.

## Evidence and limits

`protocol.json`, `provenance.json` and `sources.tar.gz` freeze settings and source
identity before measurement. Each case retains `source.json`, `relay.json`,
`sink.json`, `teardown.json` and `analysis.json`. The relay records callback entry
and outgoing publish entry/return using host `CLOCK_MONOTONIC`; its explicit copy
and allocation occur between callback and publish. End-to-end source publish →
sink callback is computed independently of the relay measurements. Hop statistics
reuse the CPU analyzer, including its warm-up exclusion and timestamp validation.

Duplicate, unknown or impossible records fail the run. Missing relay records (even
during warm-up) or
sink receipts invalidate that comparison; the remaining cases still run and the
final exit status is 2. All-lost cases retain loss counts without fabricated latency
statistics. Successful teardown requires exactly three zero peer exits, worker
death and natural zero-status descendant exits. Raw RSS/FD observations do not
establish leak freedom.

This is an explicit CPU relay inside a registered private epoch. It does not enable
or validate HERMOS's native auto-bridge, general ROS launch, external-namespace
relays, GPU Tensor bridging or real VLN/rosbag workloads. Synthetic relay overhead
does not establish the final research requirement's acceptance threshold.

Initial full evidence: `log/g5/relay-01/results/report.md`, 17,713/18,000 measured
receipts. All 12 small-payload cases completed; six 1 MiB cases lost 287 receipts
and remained invalid comparisons. See [G5 evidence](../EVIDENCE.md).

Host logic checks:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest experiments/g5/cpu_relay/test -q
```
