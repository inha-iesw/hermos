// Copyright 2026 rclpy zerocopy maintainers
// SPDX-License-Identifier: Apache-2.0
#include "runtime/runtime.hpp"
#include <atomic>
#include <stdexcept>
#include <utility>

namespace rclpy_agnocast_gpu
{
namespace
{
struct Managed
{
  DLManagedTensor tensor{};
  std::vector<std::int64_t> shape;
  std::shared_ptr<Loan> producer;
  std::unique_ptr<Payload> reader;
  std::atomic<std::size_t> references{1};
  std::atomic<bool> released{false};

  void drop_reference() noexcept
  {
    if (references.fetch_sub(1, std::memory_order_acq_rel) == 1) {delete this;}
  }
  void release() noexcept
  {
    // A consumer may invoke its deleter and then throw before renaming the
    // capsule. The separate capsule pin makes that error path idempotent.
    if (released.exchange(true, std::memory_order_acq_rel)) {return;}
    if (producer) {producer->release_export(); producer.reset();}
    if (reader) {
      auto runtime = reader->runtime;
      runtime->retire(std::move(reader));
    }
    drop_reference();
  }
};
std::unique_ptr<Managed> make_managed(const Payload & payload)
{
  auto result = std::make_unique<Managed>();
  const auto & message = payload.message;
  const auto bytes = descriptor::tensor_bytes(message);
  result->shape.assign(message.shape.begin(), message.shape.end());
  result->tensor.dl_tensor = {
    bytes ? payload.pointer() : nullptr, {kDLCUDA, payload.runtime->device()},
    static_cast<int32_t>(result->shape.size()),
    {message.dtype_code, message.dtype_bits, message.dtype_lanes},
    result->shape.data(), nullptr, 0};
  result->tensor.manager_ctx = result.get();
  result->tensor.deleter = [](DLManagedTensor * value) {
      static_cast<Managed *>(value->manager_ctx)->release();
    };
  return result;
}
}
DLManagedTensor * Loan::dlpack(std::uintptr_t stream)
{
  std::lock_guard<std::mutex> lock(mutex_);
  if (!payload_ || cancelling_ || prepared_ || exported_) {
    throw std::runtime_error("GPU loan export is single-use and requires an active loan");
  }
  if (stream_value(stream) != payload_->stream) {
    throw std::invalid_argument("DLPack consumer stream differs from the bound producer stream");
  }
  auto managed = make_managed(*payload_);
  managed->producer = shared_from_this();
  ++exports_;
  exported_ = true;
  auto tensor = &managed->tensor;
  managed.release();
  return tensor;
}
DLManagedTensor * ReadExport::dlpack(std::uintptr_t stream)
{
  if (!payload_) {throw std::runtime_error("GPU read export is single-use");}
  if (stream_value(stream) != payload_->stream) {
    throw std::invalid_argument("DLPack consumer stream differs from the bound read stream");
  }
  auto managed = make_managed(*payload_);
  managed->reader = std::move(payload_);
  auto tensor = &managed->tensor;
  managed.release();
  return tensor;
}
void retain_capsule(DLManagedTensor * tensor) noexcept
{
  static_cast<Managed *>(tensor->manager_ctx)->references.fetch_add(1, std::memory_order_relaxed);
}
void release_capsule(DLManagedTensor * tensor, bool unconsumed) noexcept
{
  auto managed = static_cast<Managed *>(tensor->manager_ctx);
  if (unconsumed) {managed->release();}
  managed->drop_reference();
}
}  // namespace rclpy_agnocast_gpu
