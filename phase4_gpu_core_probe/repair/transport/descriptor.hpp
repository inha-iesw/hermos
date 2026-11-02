#pragma once

#include "rclpy_hermos_gpu/descriptor/validate.hpp"

// Preserve the probe interface while production owns the shared wire validator.
namespace g3_transport
{
using Message = rclpy_hermos_gpu::descriptor::Message;
using rclpy_hermos_gpu::descriptor::tensor_bytes;
}  // namespace g3_transport
