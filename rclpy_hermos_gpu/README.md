# Python CUDA Tensor adapter

> Naming migration: these commands use HERMOS. Existing EVIDENCE and device logs
> describe the pre-rename Agnocast binaries. Rebuild all peers/providers and the
> device artifact bundle at `build/hermos/device-arm64` before a new device run;
> do not reuse or relabel the old G2 manifest. See the [migration guide](../README.md).

This package connects the adopted CUDA VMM core to the existing typed HERMOS
Node/Waitable path. The CUDA core is linked into exactly one `_native` module per
process. The generated `_types` provider contains only descriptor operations.

The first G4 adapter is implemented and validated on Thor under the documented
direct-peer/private-namespace contract. Actual CUDA ownership, Python/C++ transport,
async/crash shutdown, DDS coexistence and a bounded zero-copy trace are recorded in
[EVIDENCE.md](EVIDENCE.md). Logs and source hashes are under `log/g4/`. Performance,
physical VRAM accounting, Orin and general ROS launch remain outside this result.

## Runtime and endpoint contract

Initialize one process-wide `GpuRuntime`, create endpoints, then call `register()`
before any GPU work or executor dispatch. The runtime owns a single producer pool,
shared mapper limits and shutdown. `block_count=0` selects a consumer-only peer.
Configuration is immutable and initialization is one-shot even if it fails.
Restart a complete supervised peer group to start another epoch.

```python
from rclpy_hermos import Node, SubscriptionLimits
from rclpy_hermos_gpu import GpuRuntime
from rclpy_hermos_gpu_msgs.msg import GpuTensor
from rclpy.qos import QoSProfile, ReliabilityPolicy

runtime = GpuRuntime(block_count=4, block_bytes=4096, device=0,
                     max_reads=8, max_message_bytes=4096).initialise()
# rclpy must already be initialized when constructing a Node.
node = Node('tensor_writer')
qos = QoSProfile(depth=4, reliability=ReliabilityPolicy.BEST_EFFORT)
publisher = node.create_publisher(
    GpuTensor, '/tensor', qos, transport='hermos', max_message_bytes=4096)
runtime.register()
```

The Node's `max_message_bytes` and `SubscriptionLimits` account **CPU descriptor
storage**. They do not bound the payload's GPU allocation. Runtime `block_bytes`,
`block_count`, `max_reads` and `max_message_bytes` bound the separate core resources.

### Producer

```python
import torch

stream = torch.cuda.Stream(device=0)
with publisher.borrow_tensor((32, 32), torch.float32, 'cuda:0', stream) as loan:
    tensor = loan.as_tensor()
    with torch.cuda.stream(stream):
        tensor.fill_(7)
    del tensor
    publisher.publish(loan)
```

The Tensor points directly into the core's reservation. There is no payload staging
or automatic clone/contiguous/CPU fallback. The first facade supports dense layout,
offset zero, explicit CUDA device, scalar/empty shapes, and the supported DLPack
integer/unsigned/float dtypes. Invalid dtype, shape overflow, byte bounds, device
and stream requests are rejected.

The final writable Tensor storage alias, including slice/detach, must be released
before publishing. `as_tensor()` exports once. Context exit/cancel defers reclamation
until all aliases and bound-stream work finish. Successful core preparation commits
the publication: a subsequent descriptor allocation/fill/publish error cannot
cancel that GPU generation or make it immediately reusable. The CPU descriptor is
borrowed only after preparation. Structural resizes, publication and native
lifetime operations stay on the typed worker; allocated scalar fields and scoped
metadata byte views are filled on the caller thread. Every view is released
before publication.

### Consumer

```python
consumer_stream = torch.cuda.Stream(device=0)

def consume(lease):
    tensor = lease.as_tensor(stream=consumer_stream)
    with torch.cuda.stream(consumer_stream):
        # Enqueue read-only work. A retained slice/detach keeps the read owner alive.
        result = tensor.sum()

subscription = node.create_subscription(
    GpuTensor, '/tensor', consume, qos, transport='hermos',
    limits=SubscriptionLimits(max_messages=4, max_retained_bytes=16384,
                              max_message_bytes=4096))
```

Receiving or retaining a descriptor does not pin its GPU generation. The native
read is acquired only when DLPack export is requested; an obsolete generation can
be rejected. Each lease exports once. The DLPack owner retains the native read
independently of the callback and CPU descriptor. An unconsumed capsule releases
that same owner exactly once.

Treat consumer Tensors as read-only. The adopted core's CUDA mapping grants
READWRITE access; CPU shared-heap read-only protection does not apply to it.
This facade does not claim to enforce immutable CUDA Tensor writes.

### Streams and close

`as_tensor()` consumes DLPack inside the supplied Torch stream context and checks
the actual `__dlpack__(stream=...)` request. Raw CUDA stream 0 and DLPack legacy
stream 1 refer to the same default stream. Per-thread default stream 2 and streams
from another primary context are rejected.

One export has one bound stream. If an alias is used on another stream, explicitly
join that stream's completion into the bound stream **before releasing the last
alias**. `Tensor.record_stream()` alone does not manage this external pool. Keep
caller-owned streams alive until the runtime has drained pending owners.

Endpoint close stops that endpoint and fails retryably while aliases or GPU work
remain. It never seals the shared mapper. Runtime shutdown stops all dispatch,
seals/drains the mapper, closes the producer pool, then releases its CUDA context
retain. Busy or poison results are failures; no live-pool reset is performed.
Normal alias retirement uses preallocated intrusive pending records and queries
per-owner completion events without device-wide synchronization.

For an executor owned by the caller, `shutdown(timeout=3.0, progress=...)` accepts a
nonblocking callback that advances already-started asynchronous work while owners
remain busy. The caller must stop new callback admission. Progress exceptions
propagate. The direct launcher supplies this policy: existing coroutine continuations
and client replies can finish, while new timer, subscription, service and Waitable
callbacks are blocked. A Future depending on a new application callback can time out.

`runtime.stats` separates wrapper active/pending/high-water owner counts, wrapper
event count and configured pool bytes. These counters exclude the core's internal
cached mappings/events and are not physical VRAM measurements.

## Direct supervised launcher

Use the existing [private-epoch supervisor](../phase4_gpu_core_probe/repair/recovery/README.md)
with a direct command such as:

```text
python3 -m rclpy_hermos_gpu.launch \
  --blocks 4 --block-bytes 4096 --max-reads 8 --max-message-bytes 4096 \
  application:factory application-arguments
```

The supervisor remains source-tree-only under `phase4_gpu_core_probe/repair/recovery`;
it is not installed by this package. The current supported setup requires this
workspace and its verified device artifacts. Before the first release deployable
without the source tree, the supervisor/protocol package boundary must be decided
and revalidated. The privileged bootstrap stays at its root-owned system path;
a caller-writable colcon install cannot replace that entry point. CPU-only
registered launchers and general ROS launch remain separate work.

`factory(runtime, arguments)` creates and returns one rclpy Node or a sequence of
Nodes. Construct Nodes with `context=runtime.ros_context`. The launcher initializes
the GPU runtime before calling the factory, then registers its full inventory
before starting its single-threaded executor. The factory must not spin or publish.
The launcher handles TERM/INT and checks terminal poison even without callbacks.

The peer shutdown budget is three seconds; the supervisor TERM budget is five.
A signal-wakeup observer also bounds GIL-releasing native FD calls that do not
return to Python. If the peer cannot finish teardown, it reports incomplete
teardown and exits 70. This failure path relies on whole-group process recovery;
it is not successful CUDA drain. Native code that monopolizes the GIL remains
subject to the supervisor's KILL failure boundary.

All GPU holders and HERMOS peers must belong to that registered group. Set
`HERMOS_BRIDGE_MODE=off` and `HERMOS_NO_DISCOVERY_AGENT=1` in every peer. Use the
verified heaphook/library/device environment for typed HERMOS endpoints; put
`LD_PRELOAD` on the peers, never on the supervisor. General `ros2 run/launch`,
unregistered stock HERMOS peers and adoption of an old stage remain unsupported.
DDS-only outside peers may exchange ordinary DDS data. Cyclone DDS uses its default
transport. Fast DDS defaults work within one private epoch, but the tested default
configuration timed out across different `/dev/shm` mounts. For Fast DDS communication
across that boundary, explicitly set this profile in **both** inside and outside peers:

```bash
export FASTRTPS_DEFAULT_PROFILES_FILE="$(ros2 pkg prefix rclpy_hermos_gpu)/share/rclpy_hermos_gpu/config/dds/fastdds_udp.xml"
```

This profile selects UDPv4 and disables builtin transports, including SHM. It is
never enabled as an automatic fallback. GPU descriptors never cross the registered
group boundary, and the HERMOS bridge remains disabled.

## Build and verification

Run `bash scripts/verify` to install the CPU adapter in `install/hermos/verify/`, then follow the
[G3 repair instructions](../phase4_gpu_core_probe/repair/README.md#reproduce) **through
the core/messages build only**. Build G4 next; its installed validator is needed by
the subsequent G3 probe build. Finally build the probe used by the C++ transport
tests and run the host tests:

```bash
source /opt/ros/humble/setup.bash
source install/hermos/verify/local_setup.bash
source install/hermos/gpu-core/local_setup.bash
CMAKE_BUILD_PARALLEL_LEVEL=2 colcon build --packages-select rclpy_hermos_gpu \
  --build-base build/hermos/gpu-test --install-base install/hermos/gpu-test --symlink-install \
  --cmake-args -DCMAKE_BUILD_TYPE=Debug -DBUILD_TESTING=ON
source install/hermos/gpu-test/local_setup.bash
CMAKE_BUILD_PARALLEL_LEVEL=2 colcon --log-base log/hermos/g3/repair/probe-build build \
  --base-paths phase4_gpu_core_probe --build-base build/hermos/repair-probe \
  --install-base install/hermos/repair-probe \
  --cmake-args -DG3_REPAIRED_CORE=ON -DBUILD_TESTING=ON \
    -DCMAKE_BUILD_TYPE=Debug -DCMAKE_CUDA_ARCHITECTURES=110
ctest --test-dir build/hermos/gpu-test/rclpy_hermos_gpu --output-on-failure
```

The default run skips actual-device integration. Device runs must use a **new
absolute stage**, the canonical session runner, `G4_RUN_DEVICE=1`, and the integration
directory on `PYTHONPATH` (provided by CTest). Select a fresh verified bundle with
`RCLPY_HERMOS_DEVICE_BUNDLE`; its `lib/` supplies the peer hook and library.
See the tests under `test/integration`:
native stream/alias/generation cases, public Python/C++ transport, DDS topic and
parameter traffic under both RMWs, and terminal poison/FD-stall failure control.
Their oracles check peer exit codes as well as verified teardown; worker exit 0
alone does not prove that a peer succeeded. Test logs and failed trials are retained.

The vendored DLPack header and its license are under `third_party/dlpack`, matching
the CPU adapter's pinned header. Capsule ownership follows the
[DLPack Python specification](https://dmlc.github.io/dlpack/latest/python_spec.html).
