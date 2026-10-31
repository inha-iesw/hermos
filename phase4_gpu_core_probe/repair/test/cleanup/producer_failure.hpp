#pragma once

#include <array>
#include <cstddef>

namespace g3_cleanup
{
enum class Operation : std::size_t
{
  EventDestroy, MemUnmap, MemAddressFree, MemRelease, EventQuery,
  ContextPush, ContextPop, MemSetAccess, MemCreate, PrimaryRetain,
  MemAddressReserve, MemMap, MemExport, EventCreate, IpcGetEventHandle,
  Socket, Bind, Listen, PrimaryRelease, Granularity, MetadataTruncate,
  MetadataMap, MetadataUnmap, SocketFallback, ListenerSetFd, ExportGetFd,
  ExportSetFd, ThreadCreate, SocketCollision, MetadataAllocation, ThreadAllocation, Count
};

using Counts = std::array<unsigned, static_cast<std::size_t>(Operation::Count)>;

// Each executable invocation selects one case; failures must never persist
// across independent cases in a shared CUDA process epoch.
// CUDA counters are process-wide. PrimaryRelease only observes calls; it never
// injects a failure. occurrence is relative to the counter when arm() is called.
void arm(Operation operation, unsigned occurrence, bool rollback, bool pop_mismatch);
Counts counts();
unsigned injections(Operation operation);
unsigned count(const Counts & snapshot, Operation operation);
// Starts owned-name recording and libc fault observation after CUDA warm-up.
// Socket counts process-wide AF_UNIX stream calls, including other libraries;
// Bind/Listen select only /tmp/cuda_memory_pool_ paths. A driver/MPS configuration
// with stream sockets in this interval requires validating that selection again.
// Metadata calls select the dev/inode of a successfully created owned shm FD;
// reused descriptor numbers alone never select unrelated driver mappings.
void record_owned_names();
std::size_t owned_metadata_count();
std::size_t owned_socket_count();
bool owned_names_absent();
bool unlink_owned_names();
// Inspect still-open exported/listening FDs without consuming fault counters.
bool owned_fds_cloexec();
// The selected UUID generation returns a test-owned pre-existing socket name.
void collision_uuid(const std::array<unsigned char, 16> & value);
}  // namespace g3_cleanup
