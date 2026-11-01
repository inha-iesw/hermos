#pragma once

#include <algorithm>
#include <cstdint>
#include <limits>
#include <stdexcept>

#include "rclpy_agnocast_gpu_msgs/msg/gpu_tensor.hpp"

namespace rclpy_agnocast_gpu::descriptor
{
using Message = rclpy_agnocast_gpu_msgs::msg::GpuTensor;

/**
 * @brief Validate the dense wire layout before acquiring GPU ownership.
 * @param message Descriptor with element strides and a byte offset.
 * @return Logical payload bytes; zero for an empty Tensor.
 * @throws std::invalid_argument for inconsistent or overflowing metadata.
 * @note Nonzero aligned offsets remain valid on the wire. The Python facade
 * separately restricts exports to offset zero. No GPU access is performed.
 */
inline uint64_t tensor_bytes(const Message & message)
{
  const auto invalid = []() {throw std::invalid_argument("invalid dense GPU Tensor metadata");};
  const auto bits = message.dtype_bits;
  if (message.dtype_lanes != 1 || message.dtype_code > 2 ||
    (bits != 8 && bits != 16 && bits != 32 && bits != 64) ||
    (message.dtype_code == 2 && bits == 8))
  {
    invalid();
  }
  const uint64_t item_size = bits / 8;
  if (message.allocation_bytes != message.core.byte_size ||
    message.byte_offset > message.allocation_bytes || message.byte_offset % item_size != 0 ||
    (!message.strides.empty() && message.strides.size() != message.shape.size()))
  {
    invalid();
  }
  uint64_t volume = 1;
  bool empty = false;
  for (std::size_t index = message.shape.size(); index > 0; --index) {
    const auto extent = message.shape[index - 1];
    if (extent < 0) {invalid();}
    if (!message.strides.empty() && extent != 1 &&
      message.strides[index - 1] != static_cast<int64_t>(volume))
    {
      invalid();
    }
    empty = empty || extent == 0;
    const auto factor = static_cast<uint64_t>(std::max<int64_t>(extent, 1));
    if (volume > static_cast<uint64_t>(std::numeric_limits<int64_t>::max()) / factor) {
      invalid();
    }
    volume *= factor;
  }
  if (empty) {volume = 0;}
  if (volume > std::numeric_limits<uint64_t>::max() / item_size) {invalid();}
  const auto bytes = volume * item_size;
  if (bytes > message.allocation_bytes - message.byte_offset) {invalid();}
  return bytes;
}
}  // namespace rclpy_agnocast_gpu::descriptor
