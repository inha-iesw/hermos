// Copyright 2026 rclpy zerocopy maintainers
// SPDX-License-Identifier: Apache-2.0
#include "runtime.hpp"
#include <atomic>
#include <limits>
#include <stdexcept>
#include <utility>

namespace rclpy_hermos_gpu
{
namespace
{
std::atomic<bool> epoch_started{false};
std::atomic<bool> context_failed{false};
}
void check_cuda(CUresult result, const char * operation)
{
  if (result != CUDA_SUCCESS) {
    throw std::runtime_error(std::string(operation) + ": CUDA error " + std::to_string(result));
  }
}
Context::Context(CUcontext context)
{
  const auto result = cuCtxPushCurrent(context);
  if (result != CUDA_SUCCESS) {context_failed.store(true);}
  check_cuda(result, "cuCtxPushCurrent");
}
Context::~Context()
{
  CUcontext previous{};
  if (cuCtxPopCurrent(&previous) != CUDA_SUCCESS) {context_failed.store(true);}
}
CUstream stream_value(std::uintptr_t value)
{
  if (value == 2) {throw std::invalid_argument("per-thread CUDA streams are unsupported");}
  // Torch exposes raw zero; DLPack uses 1 for the same legacy stream.
  return value <= 1 ? CU_STREAM_LEGACY : reinterpret_cast<CUstream>(value);
}

Payload::~Payload()
{
  // The runtime's primary context outlives both core handles and this event.
  read.reset();
  block.reset();
  if (completion) {
    if (cuCtxPushCurrent(runtime->context()) == CUDA_SUCCESS) {
      if (cuEventDestroy(completion) != CUDA_SUCCESS) {context_failed.store(true);}
      CUcontext previous{};
      if (cuCtxPopCurrent(&previous) != CUDA_SUCCESS) {context_failed.store(true);}
    } else {context_failed.store(true);}
  }
}
void * Payload::pointer() const
{
  return block ? block->device_ptr() : read ? read->device_ptr() : nullptr;
}
Runtime::Runtime(std::size_t blocks, std::uint64_t bytes, int device,
  std::size_t max_reads, std::uint64_t max_message_bytes)
: blocks_(blocks), bytes_(bytes), device_(device), max_reads_(max_reads),
  max_message_bytes_(max_message_bytes)
{
  if (!bytes || device < 0 || !max_reads || !max_message_bytes ||
    (blocks && (bytes > max_message_bytes || blocks > UINT64_MAX / bytes)))
  {
    throw std::invalid_argument("invalid GPU runtime capacity/device configuration");
  }
}
Runtime::~Runtime()
{
  mapper_.reset();
  manager_.reset();
  if (context_ && cuDevicePrimaryCtxRelease(device_) != CUDA_SUCCESS) {
    context_failed.store(true);
  }
}
void Runtime::initialise()
{
  std::lock_guard<std::mutex> lock(mutex_);
  if (initialised_ || closing_ || closed_ || epoch_started.exchange(true)) {
    throw std::runtime_error("GPU runtime is one epoch per process; restart required");
  }
  check_cuda(cuInit(0), "cuInit");
  check_cuda(cuDevicePrimaryCtxRetain(&context_, device_), "cuDevicePrimaryCtxRetain");
  try {
    Context context(context_);
    mapper_ = std::make_unique<Mapper>(Mapper::Config{max_reads_, max_message_bytes_});
    if (blocks_) {
      manager_ = std::make_unique<Manager>(Manager::Config{blocks_, bytes_, device_});
      if (!manager_->initialise()) {throw std::runtime_error("GPU pool initialise failed");}
    }
    initialised_ = true;
  } catch (...) {
    failed_ = true;
    throw;
  }
}
std::vector<std::string> Runtime::owned_ipc_paths()
{
  std::lock_guard<std::mutex> lock(mutex_);
  check_admission();
  return manager_ ? manager_->owned_ipc_paths() : std::vector<std::string>{};
}
void Runtime::check_admission() const
{
  if (!initialised_ || closing_ || closed_ || poisoned_locked())
  {
    throw std::runtime_error("GPU runtime is not admitting work; shutdown/restart required");
  }
}
std::unique_ptr<Payload> Runtime::payload(
  Message message, std::uintptr_t stream, std::uint64_t owner)
{
  if (!owner) {throw std::invalid_argument("GPU endpoint owner must be nonzero");}
  auto result = std::make_unique<Payload>();
  result->runtime = shared_from_this();
  result->message = std::move(message);
  result->stream = stream_value(stream);
  result->owner_id = owner;
  CUcontext stream_context{};
  check_cuda(cuStreamGetCtx(result->stream, &stream_context), "cuStreamGetCtx");
  if (stream_context != context_) {
    throw std::invalid_argument("GPU stream must use the configured device primary context");
  }
  check_cuda(cuEventCreate(&result->completion, CU_EVENT_DISABLE_TIMING), "cuEventCreate");
  owners_.try_emplace(owner, 0);
  return result;
}
std::shared_ptr<Loan> Runtime::borrow(std::vector<std::int64_t> shape, unsigned code,
  unsigned bits, std::uintptr_t stream, std::uint64_t owner)
{
  Message message;
  if (code > 2 || bits > UINT8_MAX || shape.size() > INT32_MAX) {
    throw std::invalid_argument("unsupported GPU Tensor dtype or rank");
  }
  message.dtype_code = code;
  message.dtype_bits = bits;
  message.dtype_lanes = 1;
  message.shape = std::move(shape);
  message.allocation_bytes = message.core.byte_size = bytes_;
  message.core.device_id = device_;
  (void)descriptor::tensor_bytes(message);
  std::lock_guard<std::mutex> lock(mutex_);
  check_admission();
  poll_locked();
  check_admission();
  if (!manager_) {throw std::runtime_error("consumer-only runtime cannot borrow GPU storage");}
  Context context(context_);
  auto value = payload(std::move(message), stream, owner);
  try {
  // Allocate the owner before acquiring the reservation; every failure after
  // acquisition then has an existing RAII owner and a no-allocation cancel path.
  auto result = std::make_shared<Loan>(std::move(value));
  // The loan constructor is deliberately inert. Admission is completed below.
  auto block = manager_->acquire_for_publish(stream_value(stream));
  if (!block) {
    throw std::runtime_error(manager_->poisoned() ? "GPU pool poisoned" : "GPU pool full");
  }
  // Loan is not published to another thread yet.
  result->payload_->block.emplace(std::move(*block));
  admit_owner_locked(owner);
  return result;
  } catch (...) {
    const auto found = owners_.find(owner);
    if (found != owners_.end() && !found->second) {owners_.erase(found);}
    throw;
  }
}
std::unique_ptr<ReadExport> Runtime::map(
  Message message, std::uintptr_t stream, std::uint64_t owner)
{
  (void)descriptor::tensor_bytes(message);
  if (message.byte_offset || message.core.device_id != static_cast<unsigned>(device_) ||
    message.shape.size() > INT32_MAX)
  {
    throw std::invalid_argument("GPU facade requires offset zero and the configured device");
  }
  std::lock_guard<std::mutex> lock(mutex_);
  check_admission();
  poll_locked();
  check_admission();
  Context context(context_);
  auto value = payload(std::move(message), stream, owner);
  try {
  auto result = std::make_unique<ReadExport>(std::move(value));
  auto read = mapper_->map(result->payload_->message.core, result->payload_->stream);
  if (!read) {
    throw std::runtime_error(mapper_->poisoned() ? "GPU mapper poisoned" : "GPU read rejected");
  }
  result->payload_->read.emplace(std::move(*read));
  admit_owner_locked(owner);
  return result;
  } catch (...) {
    const auto found = owners_.find(owner);
    if (found != owners_.end() && !found->second) {owners_.erase(found);}
    throw;
  }
}
void Runtime::retire(std::unique_ptr<Payload> value) noexcept
{
  if (!value || (!value->block && !value->read)) {return;}
  std::lock_guard<std::mutex> lock(mutex_);
  // Retain ownership on recording failure. Only whole-process recovery may
  // reclaim it; a failed event must never turn into early block reuse.
  CUresult status = cuCtxPushCurrent(context_);
  if (status == CUDA_SUCCESS) {
    status = cuEventRecord(value->completion, value->stream);
    CUcontext previous{};
    const auto pop = cuCtxPopCurrent(&previous);
    if (pop != CUDA_SUCCESS) {status = pop;}
  }
  value->event_failed = status != CUDA_SUCCESS;
  failed_ = failed_ || value->event_failed;
  value->next = std::move(pending_);
  pending_ = std::move(value);
  ++pending_count_;
}
void Runtime::prepared(std::unique_ptr<Payload> value) noexcept
{
  std::lock_guard<std::mutex> lock(mutex_);
  const auto owner = value->owner_id;
  // Event/context cleanup must finish before another thread observes no owners
  // and closes the runtime. Function parameters outlive local lock guards.
  value.reset();
  release_owner_locked(owner);
}
void Runtime::admit_owner_locked(std::uint64_t owner)
{
  // payload() reserves the entry before acquiring any core ownership.
  ++owners_.at(owner);
  peak_ = std::max(peak_, ++active_);
}
void Runtime::release_owner_locked(std::uint64_t owner)
{
  auto found = owners_.find(owner);
  if (--found->second == 0) {owners_.erase(found);}
  --active_;
}
void Runtime::poll_locked()
{
  if (!context_) {return;}
  Context context(context_);
  auto * link = &pending_;
  while (*link) {
    auto & value = **link;
    const auto result = value.event_failed ? CUDA_ERROR_UNKNOWN : cuEventQuery(value.completion);
    if (result == CUDA_SUCCESS) {
      auto done = std::move(*link);
      *link = std::move(done->next);
      release_owner_locked(done->owner_id);
      --pending_count_;
      // Destroying the core owner now records its completion on the same stream.
      done.reset();
    } else {
      if (result != CUDA_ERROR_NOT_READY) {failed_ = true; value.event_failed = true;}
      link = &value.next;
    }
  }
}
void Runtime::poll() {std::lock_guard<std::mutex> lock(mutex_); poll_locked();}
void Runtime::seal()
{
  std::lock_guard<std::mutex> lock(mutex_);
  closing_ = true;
  poll_locked();
  if (mapper_) {(void)mapper_->try_close();}
}
bool Runtime::poisoned() const
{
  std::lock_guard<std::mutex> lock(mutex_);
  return poisoned_locked();
}
bool Runtime::poisoned_locked() const
{
  return failed_ || context_failed.load() || (manager_ && manager_->poisoned()) ||
         (mapper_ && mapper_->poisoned());
}
bool Runtime::closing() const {std::lock_guard<std::mutex> lock(mutex_); return closing_;}
std::size_t Runtime::owner_active(std::uint64_t owner)
{
  std::lock_guard<std::mutex> lock(mutex_);
  poll_locked();
  const auto found = owners_.find(owner);
  return found == owners_.end() ? 0 : found->second;
}
Snapshot Runtime::stats()
{
  std::lock_guard<std::mutex> lock(mutex_);
  poll_locked();
  return {active_, pending_count_, peak_, active_, blocks_ * bytes_};
}
bool Runtime::try_close()
{
  std::lock_guard<std::mutex> lock(mutex_);
  if (closed_) {return true;}
  closing_ = true;
  poll_locked();
  // Seal mapper admission even while Python aliases retain their read handles.
  const bool drained = !mapper_ || mapper_->try_close();
  if (poisoned_locked() || active_ || !drained) {return false;}
  if (manager_ && !manager_->try_close()) {return false;}
  mapper_.reset();
  manager_.reset();
  if (context_) {
    // A failed release has an ambiguous ownership result. Never retry that
    // retain from the destructor; doing so could consume another owner's ref.
    context_ = nullptr;
    const auto result = cuDevicePrimaryCtxRelease(device_);
    if (result != CUDA_SUCCESS) {failed_ = true; return false;}
  }
  closed_ = true;
  return true;
}

}  // namespace rclpy_hermos_gpu
