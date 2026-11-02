# HERM0S

**Heterogeneous ROS 2 Messaging with Zero-Copy Support**

HERM0S (헤르모스, using the digit `0`) provides shared CPU messages and CUDA Tensor
access for ROS 2 processes using Python/rclpy and C++. The research motivation is
reducing serialization and payload-copy costs in edge VLN sensor and feature
pipelines. Heterogeneous refers to CPU/GPU processing and Python/C++ nodes; it
does not imply that CPU-to-GPU transfers themselves require no data movement.

The CPU path uses a shared-heap backend and generated ROSIDL adapters. The GPU
path exchanges descriptors for CUDA IPC storage and retains native owners until
Tensor aliases and their bound-stream work finish. GPU descriptor transport can
use HERM0S or DDS without changing the CUDA payload-sharing mechanism.

## Packages and source layout

| Source directory | Responsibility / ROS package |
|---|---|
| `hermos/` | CPU shared-memory backend: `hermoslib`, shared heap hook, kernel module, C++ API and tooling |
| `rclpy_zc/` | `rclpy_hermos`: Python Node facade and native message/Tensor owners |
| `rclpy_zc_msgs/` | `rclpy_hermos_msgs`: shared-buffer interface |
| `rclpy_hermos_codegen/` | ROSIDL adapter generation and provider discovery |
| `rclpy_hermos_adapters/` | Generated adapters for supported ROS message types |
| `rclpy_hermos_gpu/`, `rclpy_hermos_gpu_msgs/` | CUDA Tensor API and descriptor interface |
| `rclpy_hermos_test_msgs/`, `rclpy_hermos_test_adapters/` | Schema fixtures and generated test providers |
| `phase2_hermos_probe/`, `phase4_gpu_core_probe/` | CPU integration probe and adopted CUDA core/recovery verification |
| `experiments/g5/` | Synthetic CPU, GPU and relay benchmarks |

The `rclpy_zc/` and `rclpy_zc_msgs/` source directory names remain unchanged.
Their installed ROS packages and Python modules use `rclpy_hermos` names.

## Name migration

This is a breaking rename. Update application imports, transport selections,
build dependencies and launch environments together; there are no old-name
compatibility aliases.

This README uses **HERM0S** as the display name. Code, package names, imports and
paths use `hermos` with the letter `o`.
Use `HermosRuntime` for the class and `HERMOS_*` for environment variables.

| Interface | Identifier |
|---|---|
| Python / ROS packages | `rclpy_hermos*` |
| Runtime class | `HermosRuntime` |
| Transport selection | `transport='hermos'` |
| C++ namespace / package | `hermos::` / `hermoslib` |
| Shared libraries | `libhermos.so`, `libhermos_heaphook.so` |
| Environment variables | `HERMOS_*`, `RCLPY_HERMOS_*` |
| Owned test module/device | `hermos_rclpy` |

For example, the public imports are now:

```python
from rclpy_hermos import Node, HermosRuntime, SubscriptionLimits
from rclpy_hermos_gpu import GpuRuntime
from rclpy_hermos_gpu_msgs.msg import GpuTensor
```

Use `transport='hermos'` on the facade's publisher/subscription factories.
The [GPU API examples](rclpy_hermos_gpu/README.md) describe initialization,
borrowing, publication, consumer access and shutdown. Existing byte limits,
pool capacity and ownership requirements still apply. Removing size-policy
limits and adding dynamic GPU allocations are separate planned changes.

Rebuild all renamed packages, generated providers and peer executables. ROS
message type names, provider symbols, header identities and IPC names changed;
old and new runtime artifacts must not be mixed. An unchanged adapter ABI
version does not make old generated providers compatible.

## Build and verification

From this workspace on the configured ROS 2 Humble development environment:

```bash
CMAKE_BUILD_PARALLEL_LEVEL=2 PYTHONDONTWRITEBYTECODE=1 bash scripts/verify
source /opt/ros/humble/setup.bash
source install/hermos/verify/local_setup.bash
```

The host gate builds into `build/hermos/verify`, installs into
`install/hermos/verify`, and records colcon logs in `log/hermos/verify`.
It runs the CPU/provider and G5 host tests. Device-dependent tests remain opt-in;
a host PASS does not establish renamed CPU IPC or CUDA transport on hardware.

G5 Release build instructions live with the [CPU](experiments/g5/cpu_latency/README.md)
and [GPU](experiments/g5/gpu_latency/README.md) scenarios. Their new artifacts use
`build/hermos/g5/` and `install/hermos/g5/`, separate from old benchmark prefixes.
The GPU host endpoint gate additionally needs the rebuilt GPU production stack.

Actual device sessions require a new `build/hermos/device-arm64/` artifact bundle
with matching source hashes, library/heap hook, `hermos_rclpy.ko` and manifest.
The historical `build/g2-device-arm64/` bundle does not validate the renamed
sources. Follow the [private-epoch operating contract](phase4_gpu_core_probe/repair/recovery/README.md)
and revalidate the new bundle before reporting device support. The root-owned
namespace bootstrap remains at its existing trusted path.

### Rename verification

The renamed sources were checked in fresh prefixes on the configured Thor/Humble
development host. Counts below are per suite and overlap; do not sum them.

| Check | Result |
|---|---|
| `scripts/verify` (14-package Debug build) | 651 PASS, 118 device opt-in SKIP |
| GPU adapter host CTest | 81 PASS, 21 device opt-in SKIP |
| G3 recovery/descriptor host targets | 159 PASS |
| Actual CPU/GPU production build scripts and provenance check | PASS |
| G5 GPU host endpoint gate, including both DDS implementations | 54 PASS |
| Backend Rust / Python CLI and discovery tests | 37 / 159 PASS |
| `hermos_rclpy.ko` compilation | PASS; module not loaded |

The final checks have no failures or errors.
CTest XML remains under `build/hermos/*/*/test_results`; production GPU host XML is
`log/hermos/g5/gpu-comparison/host-verification.xml`. These checks do not replace
device transport tests or demonstrate a performance improvement.

## Performance work and evidence

The [G5 performance improvement plan](experiments/g5/PERFORMANCE_PLAN.md) defines
measurement controls, GPU descriptor-write/bulk-access changes and CPU page
reclaim A/B evaluation. These optimizations are not implemented by the rename.
Estimated improvements are not new device measurements.

Historical [G5 evidence](experiments/g5/EVIDENCE.md), GPU `EVIDENCE.md` files and
`log/g4/` / `log/g5/` retain their original names and source/artifact identities.
They describe the earlier binaries, not a rerun of the renamed stack. Real
VLN/rosbag evaluation, Orin validation and the research performance acceptance
criterion remain separate outstanding work.

## Licenses and third-party acknowledgments

The CPU backend includes modified Agnocast code for shared-heap communication,
the C++ runtime, heap hook and kernel module. The reused components are listed below.

| Component | Used in | License |
|---|---|---|
| Agnocast userspace runtime and heap hook | `hermos/src/hermoslib/`, `hermos/hermos_heaphook/` | [Apache-2.0](hermos/LICENSE) |
| Agnocast kernel module | `hermos/hermos_kmod/` | [GPL-2.0-only OR BSD-2-Clause](hermos/hermos_kmod/hermos_main.c) |
| ros2_cuda_ipc | Adopted CUDA IPC core and descriptor messages; [source pin](phase4_gpu_core_probe/upstream.lock.json) | [MIT](phase4_gpu_core_probe/repair/test/upstream/LICENSE) |
| DLPack | CPU/GPU Tensor interchange headers | [Apache-2.0](rclpy_zc/third_party/dlpack/LICENSE) |

Original license texts, copyright notices and upstream attribution are retained
with the corresponding sources. Dependencies retain their respective licenses.
