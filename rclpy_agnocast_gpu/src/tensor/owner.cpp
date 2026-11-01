// Copyright 2026 rclpy zerocopy maintainers
// SPDX-License-Identifier: Apache-2.0
#include "runtime/runtime.hpp"
#include <stdexcept>
#include <utility>
#include "ros2_cuda_ipc_core/transport/message_utils.hpp"

namespace rclpy_agnocast_gpu
{
Loan::Loan(std::unique_ptr<Payload> payload)
: payload_(std::move(payload)) {}
Loan::~Loan() {cancel();}
void Loan::cancel() noexcept
{
  std::lock_guard<std::mutex> lock(mutex_);
  cancelling_ = true;
  if (!exports_ && payload_) {
    auto runtime = payload_->runtime;
    runtime->retire(std::move(payload_));
  }
}
Message Loan::prepare()
{
  std::lock_guard<std::mutex> lock(mutex_);
  if (exports_) {throw std::runtime_error("writable GPU Tensor aliases are still active");}
  if (!payload_ || cancelling_ || prepared_) {throw std::runtime_error("GPU loan is terminal");}
  if (payload_->runtime->closing() || payload_->runtime->poisoned()) {
    throw std::runtime_error("GPU runtime is closing or poisoned");
  }
  auto result = [&]() {
      Context context(payload_->runtime->context());
      return payload_->block->prepare_publish(payload_->stream);
    }();
  if (!result) {throw std::runtime_error("GPU prepare failed; process restart required");}
  prepared_ = true;
  // Success commits the block before any middleware operation. Copy/fill errors
  // must release only this wrapper, never roll the committed reservation back.
  auto value = std::move(payload_);
  Message message = std::move(value->message);
  auto runtime = value->runtime;
  runtime->prepared(std::move(value));
  ros2_cuda_ipc_core::transport::fill_buffer_core_message(result.value(), message.core);
  return message;
}
void Loan::release_export() noexcept
{
  std::lock_guard<std::mutex> lock(mutex_);
  --exports_;
  if (cancelling_ && !exports_ && payload_) {
    auto runtime = payload_->runtime;
    runtime->retire(std::move(payload_));
  }
}
std::size_t Loan::exports() const {std::lock_guard<std::mutex> lock(mutex_); return exports_;}
std::string Loan::state() const
{
  std::lock_guard<std::mutex> lock(mutex_);
  return prepared_ ? "PREPARED" : cancelling_ ? "CANCELLED" : "BORROWED";
}
std::uintptr_t Loan::pointer() const
{
  std::lock_guard<std::mutex> lock(mutex_);
  return payload_ ? reinterpret_cast<std::uintptr_t>(payload_->pointer()) : 0;
}
ReadExport::ReadExport(std::unique_ptr<Payload> payload)
: payload_(std::move(payload)) {}
ReadExport::~ReadExport()
{
  if (payload_) {auto runtime = payload_->runtime; runtime->retire(std::move(payload_));}
}
std::uintptr_t ReadExport::pointer() const
{
  return payload_ ? reinterpret_cast<std::uintptr_t>(payload_->pointer()) : 0;
}
}  // namespace rclpy_agnocast_gpu
