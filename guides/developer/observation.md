# Metadata and native statistics

HERM0S observation reads endpoint metadata and kernel counters. It does not
subscribe to application payloads, borrow messages, retain aliases, or enable a
DDS payload bridge. `ros2 topic hz_hermos` measures the DDS bridge's receive
rate; use `stats_hermos` for native cumulative counters.

Build the workspace with `scripts/verify` and source the installed overlay.
The administrator must prepare the matching kernel module and device as in
[setup](setup.md). The CLI uses `HERMOS_DEVICE_PATH` for local queries; the
workspace installation uses `/dev/hermos_rclpy`.

## Observe a registered group

Add this peer to the group's existing `HermosPeerGroup(peers=[...])`:

```python
HermosPeer(
    peer_id='observer', role='metadata', package='hermos_runtime',
    executable='hermos_metadata_peer', registered=True,
)
```

The installed observer selects the device from the runtime installation
manifest and registers an empty resource inventory. It starts publishing metadata
only after the supervisor acknowledges every peer. The observer has no heap-hook
preload and loads no payload runtime or CUDA. It does not claim the bridge agent's
kernel slot, so observation and bridging agents can run together. The supervisor
owns observation startup, termination, and restart. It rejects multiple metadata
peers in one group with the same effective ROS domain before creating the stage
or starting children. A peer's `env.ROS_DOMAIN_ID` overrides the supervisor's
value; unset or empty values mean 0. Metadata peers require canonical decimal
notation in the uint32 range: `0` or a nonzero digit followed by decimal digits.
Leading zeros (`07`, `010`), hexadecimal, signs, and whitespace are rejected so
native and DDS domain parsing cannot disagree. Different domains may each have
one metadata peer. A failed registration still fails group startup.

The registered entry requires `role='metadata'`. The supervisor overwrites the
reserved `G3_RECOVERY_ROLE` environment variable from that role; adding an env
override does not replace the role declaration. An omitted or incorrect role
fails before observer initialization. Running `hermos_metadata_peer` directly
without the supervisor role is rejected; use the standalone command below.

Every epoch receives a fresh session ID shared by its peers. Restarted epochs
must be treated as different sessions even if their node and topic names are
identical. Registration ACK is not an application-readiness signal. An
observer does not create a native payload subscriber; existing bridge
subscribers also must not be counted as ready business consumers. Use an
application-level readiness handshake when publishing requires one.

For standalone CPU nodes sharing the current IPC namespace, run an explicit
observer in a separate terminal with the same domain and selected device:

```bash
HERMOS_DEVICE_PATH=/dev/hermos_rclpy \
  ros2 run ros2hermos_discovery_agent discovery_agent --observation-only
```

This standalone process has its own observation session and no group ACK.
`--observation-only` selects this mode; `HERMOS_BRIDGE_MODE=off` alone does not.
Use the registered peer above for a private supervised group. Do not preload
the payload heap hook into either observer or querying CLI.

## Query from another terminal

Choose the same `ROS_DOMAIN_ID` as the observed agent. For Fast DDS across the
private shared-memory mount, apply the [explicit UDP profile](../../rclpy_hermos_gpu/README.md)
to both the agent's group environment and the querying terminal. No system ROS
daemon or global DDS configuration is replaced.

```bash
export ROS_DOMAIN_ID=42
export HERMOS_DEVICE_PATH=/dev/hermos_rclpy
ros2 topic list_hermos
ros2 topic info_hermos /camera/frames
ros2 topic stats_hermos /camera/frames
ros2 node list_hermos
ros2 node info_hermos /camera/reader
```

Snapshots distinguish host boot ID, IPC namespace, DDS domain, device identity,
session, and writer role. For a host/namespace/domain/device route, default queries prefer the bridge
writer when present and otherwise use the most recently received observer
session. An explicit `--session-id` can select an observer alongside a bridge
writer. Names that occur in several scopes appear separately. Select a scope with
`--session-id`, `--ipc-namespace`, `--device-id`, and/or `--host-id`; an ambiguous `info_hermos`
query reports the ambiguity instead of summing unrelated endpoints. The
domain is the CLI's actual ROS context domain, selected by `ROS_DOMAIN_ID`.

Use `--local` for an immediate ioctl-only query within the current IPC
namespace. This cannot inspect a private group's namespace from outside it.
Local queries without a known session identify it as unknown. Remote queries
use metadata gossip and fall back to the local namespace when no snapshot is
available; a local empty result does not prove that an isolated group is empty.
Without a usable local device or wrapper, default list/info commands report a
NOTE and still show DDS results. `--local` and `stats_hermos` report the native
query failure instead. A node visible through both DDS and HERM0S has one row
per HERM0S scope, with DDS marked on that row; it is not counted twice.
Bridge diagnostics consider all namespaces in the CLI domain, even when a
session selector limits the displayed rows. Automatic bridge decisions use only
bridge-role snapshots; an observer does not request payload forwarding. CLI
queries retain the observer fallback described above. Daemon status requires a
bridge-role snapshot from the local host, namespace, domain, and selected device; an observer
cannot prove bridge availability.

Device identity is the source character device's `major:minor`, interpreted with
the host boot ID. Path aliases share an identity; `/dev/hermos` and
`/dev/hermos_rclpy` remain separate inventories when loaded together. Use
`--device-id MAJOR:MINOR` to select one. This supports simultaneous observation;
cross-device native bridging in the same namespace/domain remains unsupported
because bridge-manager sockets do not distinguish devices. Do not start bridge
managers for both devices in that scope. Device numbers do not identify module
reload generations. Unavailable host/device identity makes daemon publication
unverifiable, and the status command reports that with a nonzero exit status.

Metadata is published periodically with a 30-second lease and transient-local
durability for late readers. The receive cache expires using the querying
process's monotonic clock. The writer's lifespan bounds its retained samples;
readers have no lifespan so a remote source timestamp is not compared to the
reader's wall clock. Agent and CLI share the v2 envelope, QoS, and lease
contract. Both reject v1 and unknown schemas; all participants need matching
generated message definitions. The v2 lease is fixed at 30 seconds; readers
reject other durations.
The source wall-clock timestamp is informational, not a cross-host clock
synchronization guarantee. Unknown schema versions, unavailable types, and
unsupported or permission-denied statistics are not represented as valid zero
measurements. IPC handles, socket paths, and file descriptors are never exported.
An agent that cannot query topics skips that tick, logs the error, and retries;
it neither publishes a false empty graph nor makes bridge/idle-exit decisions
from the failed snapshot. Previously received data still expires normally.

## Counter meaning

Counters belong to a live native endpoint and reset when it is recreated. Use
the complete scope and endpoint ID when comparing samples. The sample time is
the source host's monotonic clock, so elapsed-time comparisons require the same
host boot. Samples from different endpoints are not one global atomic sample.
Endpoint IDs can be reused within a session. Restart comparisons after endpoint
recreation or a counter reset; an ID alone does not prove lifetime continuity.

| Field | Meaning |
|---|---|
| `published_total` | Successfully inserted kernel message entries |
| `received_total` | Successful kernel subscriber reference acquisitions, not callback completions |
| `dropped_history_total` | Unread eligible entries removed by publisher history pruning; not all application or transport drops |
| `queued_entries` | Eligible resident entries after the subscriber cursor, before subscriber QoS filtering |
| `retained_entries` | Publisher resident entries or subscriber-held kernel references |
| `sample_time_ns` | Kernel observation time in the source host's monotonic clock |

Only fields selected by the endpoint's support mask are available. Python
callback-queue drops, retained bytes, application completion rates, and CUDA
completion rates are not inferred from kernel counters. A metadata query must
not change the measured endpoint's queue or reference ownership.
Native publishers do not store a reliability QoS value; their reliability is
unavailable rather than inferred from the subscriber settings.

Statistics ABI v1 uses a fixed 336-byte row. The kernel checks both ABI version
and row size, and Python requires the capacity-negotiating wrapper symbol
`hermos_query_endpoint_stats_v1`. Python obtains the required row count and retries
when the supplied buffer is too small; there is no fixed-capacity fallback. An
incompatible ABI is unsupported, and invalid query arguments are unavailable;
neither becomes a zero measurement. The wrapper normalizes an older kernel's
unknown-command `EINVAL` to unsupported (`ENOTTY`).
