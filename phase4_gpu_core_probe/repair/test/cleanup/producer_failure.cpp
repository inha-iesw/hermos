#include "producer_failure.hpp"

#include <cuda.h>
#include <dlfcn.h>
#include <dirent.h>
#include <fcntl.h>
#include <pthread.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>
#include <uuid/uuid.h>

#include <atomic>
#include <algorithm>
#include <cerrno>
#include <cstdarg>
#include <cstring>
#include <string>
#include <mutex>
#include <new>
#include <vector>

namespace
{
using g3_cleanup::Operation;
constexpr auto operation_count = static_cast<std::size_t>(Operation::Count);
std::array<std::atomic<unsigned>, operation_count> observed{};
std::array<std::atomic<unsigned>, operation_count> injected{};
Operation target = Operation::Count;
unsigned target_call = 0;
unsigned access_call = 0;
unsigned fallback_call = 0;
bool return_wrong_context = false;
bool collect_names = false;
std::vector<std::string> metadata_names;
std::vector<std::string> socket_names;
struct OwnedFd
{
  int fd;
  dev_t device;
  ino_t inode;
};
std::vector<OwnedFd> metadata_fds;
std::vector<OwnedFd> export_fds;
std::vector<OwnedFd> listener_fds;
std::vector<std::pair<void *, std::size_t>> metadata_maps;
std::mutex tracked_mutex;
thread_local bool pending_server_thread = false;
thread_local Operation pending_allocation = Operation::Count;
std::array<unsigned char, 16> colliding_uuid{};

bool matches_fd(const std::vector<OwnedFd> & records, int fd)
{
  struct stat status{};
  if (!collect_names || fstat(fd, &status) != 0) {return false;}
  return std::any_of(records.begin(), records.end(), [&](const auto & owned) {
    return owned.fd == fd && owned.device == status.st_dev && owned.inode == status.st_ino;
  });
}

bool owned_metadata_fd(int fd)
{
  std::lock_guard<std::mutex> lock(tracked_mutex);
  return matches_fd(metadata_fds, fd);
}

void record_fd(std::vector<OwnedFd> & records, int fd)
{
  struct stat status{};
  if (fstat(fd, &status) == 0) {records.push_back({fd, status.st_dev, status.st_ino});}
}

unsigned observe(Operation operation)
{
  return observed[static_cast<std::size_t>(operation)].fetch_add(1) + 1;
}

bool fail(Operation operation)
{
  const auto index = static_cast<std::size_t>(operation);
  const auto current = observe(operation);
  if ((operation == target && current == target_call) ||
    (operation == Operation::MemSetAccess && current == access_call) ||
    (operation == Operation::SocketFallback && current == fallback_call))
  {
    injected[index].fetch_add(1);
    return true;
  }
  return false;
}

template<typename Function>
Function original(const char * symbol)
{
  return reinterpret_cast<Function>(dlsym(RTLD_NEXT, symbol));
}

bool pool_socket_address(const sockaddr * address, socklen_t size)
{
  constexpr char prefix[] = "/tmp/cuda_memory_pool_";
  if (size < offsetof(sockaddr_un, sun_path) + sizeof(prefix) - 1 ||
    address->sa_family != AF_UNIX) {return false;}
  const auto * local = reinterpret_cast<const sockaddr_un *>(address);
  return std::strncmp(local->sun_path, prefix, sizeof(prefix) - 1) == 0;
}
}  // namespace

extern "C" int __real_fcntl(int fd, int command, ...);
extern "C" void * __real__Znwm(std::size_t size);

// Only the first static-core allocation after an armed mmap/listen can fail.
// Bookkeeping finishes before opening the TLS window; driver and other thread
// allocations do not consume it, and throwing clears it before stack unwinding.
extern "C" void * __wrap__Znwm(std::size_t size)
{
  const auto operation = pending_allocation;
  pending_allocation = Operation::Count;
  if (operation != Operation::Count && fail(operation)) {
    if (operation == Operation::ThreadAllocation) {pending_server_thread = false;}
    throw std::bad_alloc();
  }
  return __real__Znwm(size);
}

namespace g3_cleanup
{
void arm(Operation operation, unsigned occurrence, bool rollback, bool pop_mismatch)
{
  target = operation;
  target_call = observed[static_cast<std::size_t>(operation)].load() + occurrence;
  access_call = rollback ? observed[static_cast<std::size_t>(Operation::MemSetAccess)].load() +
    1 : 0;
  return_wrong_context = pop_mismatch;
  // ListenerSetFd tests the second listener's compatibility fallback.
  fallback_call = operation == Operation::ListenerSetFd ?
    observed[static_cast<std::size_t>(Operation::SocketFallback)].load() + 2 : 0;
}

Counts counts()
{
  Counts result{};
  for (std::size_t index = 0; index < result.size(); ++index) {
    result[index] = observed[index].load();
  }
  return result;
}

unsigned injections(Operation operation)
{
  return injected[static_cast<std::size_t>(operation)].load();
}

unsigned count(const Counts & snapshot, Operation operation)
{
  return snapshot[static_cast<std::size_t>(operation)];
}

void record_owned_names() {collect_names = true;}
void collision_uuid(const std::array<unsigned char, 16> & value) {colliding_uuid = value;}
std::size_t owned_metadata_count() {return metadata_names.size();}
std::size_t owned_socket_count() {return socket_names.size();}

bool owned_names_absent()
{
  for (const auto & name : metadata_names) {
    const int fd = shm_open(name.c_str(), O_RDONLY, 0);
    if (fd >= 0) {close(fd); return false;}
    if (errno != ENOENT) {return false;}
  }
  for (const auto & name : socket_names) {
    struct stat status{};
    if (lstat(name.c_str(), &status) == 0 || errno != ENOENT) {return false;}
  }
  return true;
}

bool unlink_owned_names()
{
  bool success = true;
  for (const auto & name : metadata_names) {
    if (shm_unlink(name.c_str()) != 0 && errno != ENOENT) {
      success = false;
    }
  }
  for (const auto & name : socket_names) {
    if (unlink(name.c_str()) != 0 && errno != ENOENT) {
      success = false;
    }
  }
  return success;
}

bool owned_fds_cloexec()
{
  std::lock_guard<std::mutex> lock(tracked_mutex);
  unsigned checked = 0;
  for (const auto * records : {&export_fds, &listener_fds}) {
    for (const auto & record : *records) {
      struct stat status{};
      if (fstat(record.fd, &status) != 0 || status.st_dev != record.device ||
        status.st_ino != record.inode) {continue;}
      const int flags = __real_fcntl(record.fd, F_GETFD);
      if (flags < 0 || !(flags & FD_CLOEXEC)) {return false;}
      ++checked;
    }
  }
  return checked != 0;
}
}  // namespace g3_cleanup

// A failed operation is skipped exactly once. A retry reaches the real driver
// and is counted, so a cleanup retry cannot be hidden by a permanent failure.
extern "C" CUresult CUDAAPI cuEventDestroy(CUevent event)
{
  if (fail(Operation::EventDestroy)) {return CUDA_ERROR_UNKNOWN;}
  static const auto real = original<decltype(&cuEventDestroy)>("cuEventDestroy_v2");
  return real ? real(event) : CUDA_ERROR_UNKNOWN;
}

extern "C" CUresult CUDAAPI cuMemUnmap(CUdeviceptr pointer, std::size_t size)
{
  if (fail(Operation::MemUnmap)) {return CUDA_ERROR_UNKNOWN;}
  static const auto real = original<decltype(&cuMemUnmap)>("cuMemUnmap");
  return real ? real(pointer, size) : CUDA_ERROR_UNKNOWN;
}

extern "C" CUresult CUDAAPI cuMemAddressFree(CUdeviceptr pointer, std::size_t size)
{
  if (fail(Operation::MemAddressFree)) {return CUDA_ERROR_UNKNOWN;}
  static const auto real = original<decltype(&cuMemAddressFree)>("cuMemAddressFree");
  return real ? real(pointer, size) : CUDA_ERROR_UNKNOWN;
}

extern "C" CUresult CUDAAPI cuMemRelease(CUmemGenericAllocationHandle handle)
{
  if (fail(Operation::MemRelease)) {return CUDA_ERROR_UNKNOWN;}
  static const auto real = original<decltype(&cuMemRelease)>("cuMemRelease");
  return real ? real(handle) : CUDA_ERROR_UNKNOWN;
}

extern "C" CUresult CUDAAPI cuEventQuery(CUevent event)
{
  if (fail(Operation::EventQuery)) {return CUDA_ERROR_UNKNOWN;}
  static const auto real = original<decltype(&cuEventQuery)>("cuEventQuery");
  return real ? real(event) : CUDA_ERROR_UNKNOWN;
}

extern "C" CUresult CUDAAPI cuCtxPushCurrent(CUcontext context)
{
  if (fail(Operation::ContextPush)) {return CUDA_ERROR_UNKNOWN;}
  static const auto real = original<decltype(&cuCtxPushCurrent)>("cuCtxPushCurrent_v2");
  return real ? real(context) : CUDA_ERROR_UNKNOWN;
}

extern "C" CUresult CUDAAPI cuCtxPopCurrent(CUcontext * context)
{
  const bool selected = fail(Operation::ContextPop);
  if (selected && !return_wrong_context) {return CUDA_ERROR_UNKNOWN;}
  static const auto real = original<decltype(&cuCtxPopCurrent)>("cuCtxPopCurrent_v2");
  const CUresult result = real ? real(context) : CUDA_ERROR_UNKNOWN;
  // Pop the actual stack first; only corrupt the returned identity. This
  // distinguishes a successful driver call with a broken ownership contract.
  if (selected && return_wrong_context && result == CUDA_SUCCESS) {*context = nullptr;}
  return result;
}

extern "C" CUresult CUDAAPI cuMemSetAccess(
  CUdeviceptr pointer, std::size_t size, const CUmemAccessDesc * descriptor, std::size_t count)
{
  if (fail(Operation::MemSetAccess)) {return CUDA_ERROR_UNKNOWN;}
  static const auto real = original<decltype(&cuMemSetAccess)>("cuMemSetAccess");
  return real ? real(pointer, size, descriptor, count) : CUDA_ERROR_UNKNOWN;
}

extern "C" CUresult CUDAAPI cuMemCreate(
  CUmemGenericAllocationHandle * handle, std::size_t size,
  const CUmemAllocationProp * properties, unsigned long long flags)
{
  if (fail(Operation::MemCreate)) {return CUDA_ERROR_OUT_OF_MEMORY;}
  static const auto real = original<decltype(&cuMemCreate)>("cuMemCreate");
  return real ? real(handle, size, properties, flags) : CUDA_ERROR_UNKNOWN;
}

extern "C" CUresult CUDAAPI cuMemAddressReserve(
  CUdeviceptr * pointer, std::size_t size, std::size_t alignment,
  CUdeviceptr address, unsigned long long flags)
{
  if (fail(Operation::MemAddressReserve)) {return CUDA_ERROR_OUT_OF_MEMORY;}
  static const auto real = original<decltype(&cuMemAddressReserve)>("cuMemAddressReserve");
  return real ? real(pointer, size, alignment, address, flags) : CUDA_ERROR_UNKNOWN;
}

extern "C" CUresult CUDAAPI cuMemGetAllocationGranularity(
  std::size_t * granularity, const CUmemAllocationProp * properties,
  CUmemAllocationGranularity_flags flags)
{
  if (fail(Operation::Granularity)) {return CUDA_ERROR_INVALID_VALUE;}
  static const auto real = original<decltype(&cuMemGetAllocationGranularity)>(
    "cuMemGetAllocationGranularity");
  return real ? real(granularity, properties, flags) : CUDA_ERROR_UNKNOWN;
}

extern "C" CUresult CUDAAPI cuMemMap(
  CUdeviceptr pointer, std::size_t size, std::size_t offset,
  CUmemGenericAllocationHandle handle, unsigned long long flags)
{
  if (fail(Operation::MemMap)) {return CUDA_ERROR_OUT_OF_MEMORY;}
  static const auto real = original<decltype(&cuMemMap)>("cuMemMap");
  return real ? real(pointer, size, offset, handle, flags) : CUDA_ERROR_UNKNOWN;
}

extern "C" CUresult CUDAAPI cuMemExportToShareableHandle(
  void * handle, CUmemGenericAllocationHandle allocation,
  CUmemAllocationHandleType type, unsigned long long flags)
{
  if (fail(Operation::MemExport)) {return CUDA_ERROR_UNKNOWN;}
  static const auto real = original<decltype(&cuMemExportToShareableHandle)>(
    "cuMemExportToShareableHandle");
  const auto result = real ? real(handle, allocation, type, flags) : CUDA_ERROR_UNKNOWN;
  if (result == CUDA_SUCCESS && collect_names && type == CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR) {
    std::lock_guard<std::mutex> lock(tracked_mutex);
    record_fd(export_fds, *static_cast<int *>(handle));
  }
  return result;
}

extern "C" CUresult CUDAAPI cuEventCreate(CUevent * event, unsigned flags)
{
  if (fail(Operation::EventCreate)) {return CUDA_ERROR_OUT_OF_MEMORY;}
  static const auto real = original<decltype(&cuEventCreate)>("cuEventCreate");
  return real ? real(event, flags) : CUDA_ERROR_UNKNOWN;
}

extern "C" CUresult CUDAAPI cuIpcGetEventHandle(CUipcEventHandle * handle, CUevent event)
{
  if (fail(Operation::IpcGetEventHandle)) {return CUDA_ERROR_UNKNOWN;}
  static const auto real = original<decltype(&cuIpcGetEventHandle)>("cuIpcGetEventHandle");
  return real ? real(handle, event) : CUDA_ERROR_UNKNOWN;
}

extern "C" CUresult CUDAAPI cuDevicePrimaryCtxRetain(CUcontext * context, CUdevice device)
{
  if (fail(Operation::PrimaryRetain)) {return CUDA_ERROR_OUT_OF_MEMORY;}
  static const auto real = original<decltype(&cuDevicePrimaryCtxRetain)>("cuDevicePrimaryCtxRetain");
  return real ? real(context, device) : CUDA_ERROR_UNKNOWN;
}

extern "C" CUresult CUDAAPI cuDevicePrimaryCtxRelease(CUdevice device)
{
  (void)observe(Operation::PrimaryRelease);
  static const auto real = original<decltype(&cuDevicePrimaryCtxRelease)>("cuDevicePrimaryCtxRelease_v2");
  return real ? real(device) : CUDA_ERROR_UNKNOWN;
}

// Record names only after successful creation by this process. The PID comes
// from the same namespace as the allocator; no global orphan scan is used.
extern "C" int shm_open(const char * name, int flags, mode_t mode)
{
  static const auto real = original<decltype(&shm_open)>("shm_open");
  if (!real) {errno = ENOSYS; return -1;}
  const int result = real(name, flags, mode);
  if (result >= 0 && collect_names && (flags & O_CREAT) && (flags & O_EXCL)) {
    const auto prefix = "/rclpy_zc_gpu_v1_" + std::to_string(getpid()) + "_";
    if (std::strncmp(name, prefix.c_str(), prefix.size()) == 0) {
      metadata_names.emplace_back(name);
      std::lock_guard<std::mutex> lock(tracked_mutex);
      record_fd(metadata_fds, result);
    }
  }
  return result;
}

extern "C" int ftruncate(int fd, off_t length) noexcept
{
  if (owned_metadata_fd(fd) && fail(Operation::MetadataTruncate)) {
    errno = ENOSPC;
    return -1;
  }
  static const auto real = original<decltype(&ftruncate)>("ftruncate");
  if (!real) {errno = ENOSYS; return -1;}
  return real(fd, length);
}

extern "C" void * mmap(
  void * address, std::size_t length, int protection, int flags, int fd, off_t offset) noexcept
{
  const bool owned = owned_metadata_fd(fd);
  if (owned && fail(Operation::MetadataMap)) {errno = ENOMEM; return MAP_FAILED;}
  static const auto real = original<decltype(&mmap)>("mmap");
  if (!real) {errno = ENOSYS; return MAP_FAILED;}
  void * result = real(address, length, protection, flags, fd, offset);
  if (owned && result != MAP_FAILED) {
    std::lock_guard<std::mutex> lock(tracked_mutex);
    metadata_maps.emplace_back(result, length);
    if (target == Operation::MetadataAllocation) {pending_allocation = target;}
  }
  return result;
}

extern "C" int munmap(void * address, std::size_t length) noexcept
{
  static const auto real = original<decltype(&munmap)>("munmap");
  if (!real) {errno = ENOSYS; return -1;}
  std::lock_guard<std::mutex> lock(tracked_mutex);
  const auto owned = std::find(metadata_maps.begin(), metadata_maps.end(),
      std::make_pair(address, length));
  const int result = real(address, length);
  if (result == 0 && owned != metadata_maps.end()) {
    (void)observe(Operation::MetadataUnmap);
    metadata_maps.erase(owned);
  }
  return result;
}

extern "C" int socket(int domain, int type, int protocol) noexcept
{
  const bool selected = collect_names && domain == AF_UNIX &&
    (type & ~SOCK_CLOEXEC) == SOCK_STREAM;
  if (selected) {
    if (fail(Operation::Socket)) {errno = EMFILE; return -1;}
    if ((type & SOCK_CLOEXEC) && fail(Operation::SocketFallback)) {
      errno = EINVAL;
      return -1;
    }
  }
  static const auto real = original<decltype(&socket)>("socket");
  if (!real) {errno = ENOSYS; return -1;}
  const int result = real(domain, type, protocol);
  if (selected && result >= 0) {
    std::lock_guard<std::mutex> lock(tracked_mutex);
    record_fd(listener_fds, result);
  }
  return result;
}

extern "C" int bind(int socket, const sockaddr * address, socklen_t size) noexcept
{
  static const auto real = original<decltype(&bind)>("bind");
  if (!real) {errno = ENOSYS; return -1;}
  const bool pool_socket = collect_names && pool_socket_address(address, size);
  if (pool_socket && fail(Operation::Bind)) {errno = EADDRINUSE; return -1;}
  const int result = real(socket, address, size);
  if (result == 0 && pool_socket) {
    const auto * local = reinterpret_cast<const sockaddr_un *>(address);
    socket_names.emplace_back(local->sun_path);
  }
  return result;
}

extern "C" int listen(int socket, int backlog) noexcept
{
  sockaddr_un address{};
  socklen_t size = sizeof(address);
  if (collect_names && getsockname(socket, reinterpret_cast<sockaddr *>(&address), &size) == 0 &&
    pool_socket_address(reinterpret_cast<const sockaddr *>(&address), size) && fail(Operation::Listen))
  {
    errno = EIO;
    return -1;
  }
  static const auto real = original<decltype(&listen)>("listen");
  if (!real) {errno = ENOSYS; return -1;}
  const int result = real(socket, backlog);
  if (result == 0 && collect_names &&
    pool_socket_address(reinterpret_cast<const sockaddr *>(&address), size))
  {
    pending_server_thread = true;
    if (target == Operation::ThreadAllocation) {pending_allocation = target;}
  }
  return result;
}

// Link wrapping selects direct calls in the static core, avoiding libc/CUDA's
// unrelated variadic fcntl commands. This core uses only these two commands.
extern "C" int __wrap_fcntl(int fd, int command, ...)
{
  if (command != F_GETFD && command != F_SETFD) {errno = ENOSYS; return -1;}
  Operation operation = Operation::Count;
  {
    std::lock_guard<std::mutex> lock(tracked_mutex);
    if (matches_fd(export_fds, fd)) {
      operation = command == F_GETFD ? Operation::ExportGetFd : Operation::ExportSetFd;
    } else if (command == F_SETFD && matches_fd(listener_fds, fd)) {
      operation = Operation::ListenerSetFd;
    }
  }
  if (operation != Operation::Count && fail(operation)) {errno = EIO; return -1;}
  if (command == F_GETFD) {return __real_fcntl(fd, command);}
  va_list args;
  va_start(args, command);
  const int flags = va_arg(args, int);
  va_end(args);
  return __real_fcntl(fd, command, flags);
}

extern "C" int pthread_create(
  pthread_t * thread, const pthread_attr_t * attributes, void * (*entry)(void *), void * argument)
  noexcept
{
  const bool selected = pending_server_thread;
  pending_server_thread = false;
  if (selected && fail(Operation::ThreadCreate)) {return EAGAIN;}
  static const auto real = original<decltype(&pthread_create)>("pthread_create");
  return real ? real(thread, attributes, entry, argument) : EAGAIN;
}

extern "C" void uuid_generate(uuid_t result)
{
  if (collect_names && fail(Operation::SocketCollision)) {
    std::memcpy(result, colliding_uuid.data(), colliding_uuid.size());
    return;
  }
  static const auto real = original<decltype(&uuid_generate)>("uuid_generate");
  if (real) {real(result);}
}

// Historical cores scan unrelated shared-memory names during initialise.
// Suppress that legacy scavenger in RED runs as well as the repaired tests.
extern "C" DIR * opendir(const char * path)
{
  if (std::strcmp(path, "/dev/shm") == 0) {errno = EACCES; return nullptr;}
  static const auto real = original<decltype(&opendir)>("opendir");
  return real ? real(path) : nullptr;
}
