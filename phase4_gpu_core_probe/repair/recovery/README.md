# Owned GPU peer recovery scenario

The operational contract now belongs to
[hermos_runtime supervision](../../../hermos_runtime/README.md).
It defines administrator installation, versioned requests, registration/ACK,
shutdown, ownership evidence and restart restrictions. This directory retains
its CLI shim and the G3 scenario evidence.

The §36 host/device review corrections on Thor are historical G3 evidence in
[EVIDENCE.md](../EVIDENCE.md). Later Python GPU integration is documented in
[the GPU guide](../../../rclpy_hermos_gpu/README.md), and E2b ROS launch in
[hermos_launch](../../../hermos_launch/README.md). Those later results do not
expand the scope of the original §36 measurements.

Runtime IPC events leave driver-owned objects on the tested CUDA combination;
see [the standalone diagnostics](../residency/README.md). They and the fixed
HERMOS type registry remain inside the epoch's tmpfs until its final holder
exits. No driver name is manually unlinked, no CUDA reset is used, and no mount or
namespace FD is exported to the owner. The kernel releases the private mount
after the worker and descendants exit. Evidence inventories private residue before
that final exit; it does not claim that CUDA's internal cleanup has been repaired
or measure physical VRAM. Existing host objects are preserved. The original
no-unbounded-accumulation criterion applies to this bounded execution contract;
unisolated diagnostic runs do not satisfy it on the tested driver.
The unpublished three-block cleanup test covers registered inventory removal
after publisher SIGKILL. Short epoch tests do not establish an
event-count-independent bound during a long-running epoch, general block-count
scaling, aggregate tmpfs limits across many groups, or physical GPU-memory
recovery. The OS failure controls use synthetic non-GPU peers to test restart
refusal without introducing orphan CUDA resources; they do not establish native
CUDA daemon behavior after worker death. Those limits remain explicit until the
corresponding scenario or metric is measured.
