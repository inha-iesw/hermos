// Copyright 2026 rclpy zerocopy maintainers
// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <cuda.h>
#include <cstdint>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>
#include "ros2_cuda_ipc_core/publisher/gpu_buffer_manager.hpp"
#include "ros2_cuda_ipc_core/subscriber/buffer_mapper.hpp"
#include "rclpy_agnocast_gpu/descriptor/validate.hpp"
#include "dlpack.h"

namespace rclpy_agnocast_gpu
{
using Message = descriptor::Message;
using Manager = ros2_cuda_ipc_core::publisher::GpuBufferManager;
using Mapper = ros2_cuda_ipc_core::subscriber::BufferMapper;
using Block = ros2_cuda_ipc_core::publisher::PublishBlock;
using Read = ros2_cuda_ipc_core::subscriber::ReadHandle;
class Runtime;
class Loan;
class ReadExport;

// Allocated before acquiring GPU ownership. The intrusive pending link makes
// final-alias release independent of host allocator success.
struct Payload
{
  std::shared_ptr<Runtime> runtime;
  std::uint64_t owner_id{};
  Message message;
  CUstream stream{};
  CUevent completion{};
  std::optional<Block> block;
  std::optional<Read> read;
  std::unique_ptr<Payload> next;
  bool event_failed{false};
  ~Payload();
  void * pointer() const;
};

struct Snapshot
{
  std::size_t owners{}, pending{}, peak_owners{}, wrapper_events{};
  std::uint64_t pool_bytes{};
};

class Runtime : public std::enable_shared_from_this<Runtime>
{
public:
  Runtime(std::size_t blocks, std::uint64_t bytes, int device,
    std::size_t max_reads, std::uint64_t max_message_bytes);
  ~Runtime();
  void initialise();
  std::vector<std::string> owned_ipc_paths();
  std::shared_ptr<Loan> borrow(std::vector<std::int64_t> shape, unsigned code,
    unsigned bits, std::uintptr_t stream, std::uint64_t owner);
  std::unique_ptr<ReadExport> map(Message message, std::uintptr_t stream, std::uint64_t owner);
  void poll();
  void seal();
  bool try_close();
  bool poisoned() const;
  bool closing() const;
  std::size_t owner_active(std::uint64_t owner);
  Snapshot stats();
  int device() const {return device_;}
  CUcontext context() const {return context_;}
  void retire(std::unique_ptr<Payload> payload) noexcept;
  void prepared(std::unique_ptr<Payload> payload) noexcept;

private:
  void poll_locked();
  bool poisoned_locked() const;
  void admit_owner_locked(std::uint64_t owner);
  void release_owner_locked(std::uint64_t owner);
  void check_admission() const;
  std::unique_ptr<Payload> payload(Message message, std::uintptr_t stream, std::uint64_t owner);
  mutable std::mutex mutex_;
  const std::size_t blocks_;
  const std::uint64_t bytes_;
  const int device_;
  const std::size_t max_reads_;
  const std::uint64_t max_message_bytes_;
  CUcontext context_{};
  std::unique_ptr<Manager> manager_;
  std::unique_ptr<Mapper> mapper_;
  std::unordered_map<std::uint64_t, std::size_t> owners_;
  std::unique_ptr<Payload> pending_;
  std::size_t active_{}, pending_count_{}, peak_{};
  bool initialised_{false}, closing_{false}, closed_{false}, failed_{false};
};

class Loan : public std::enable_shared_from_this<Loan>
{
  friend class Runtime;
public:
  explicit Loan(std::unique_ptr<Payload> payload);
  ~Loan();
  DLManagedTensor * dlpack(std::uintptr_t stream);
  Message prepare();
  void cancel() noexcept;
  void release_export() noexcept;
  std::string state() const;
  std::size_t exports() const;
  std::uintptr_t pointer() const;

private:
  mutable std::mutex mutex_;
  std::unique_ptr<Payload> payload_;
  std::size_t exports_{0};
  bool exported_{false}, cancelling_{false}, prepared_{false};
};

class ReadExport
{
  friend class Runtime;
public:
  explicit ReadExport(std::unique_ptr<Payload> payload);
  ~ReadExport();
  DLManagedTensor * dlpack(std::uintptr_t stream);
  std::uintptr_t pointer() const;

private:
  std::unique_ptr<Payload> payload_;
};

CUstream stream_value(std::uintptr_t value);
void check_cuda(CUresult result, const char * operation);
class Context
{
public:
  explicit Context(CUcontext context);
  ~Context();
  Context(const Context &) = delete;
  Context & operator=(const Context &) = delete;
};
void retain_capsule(DLManagedTensor * tensor) noexcept;
void release_capsule(DLManagedTensor * tensor, bool unconsumed) noexcept;
}  // namespace rclpy_agnocast_gpu
