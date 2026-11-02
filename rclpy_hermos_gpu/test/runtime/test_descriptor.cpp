// Copyright 2026 rclpy zerocopy maintainers
// SPDX-License-Identifier: Apache-2.0
#include <gtest/gtest.h>
#include <climits>
#include "rclpy_hermos_gpu/descriptor/validate.hpp"

namespace
{
using namespace rclpy_hermos_gpu::descriptor;
Message tensor()
{
  Message value;
  value.dtype_code = 2;
  value.dtype_bits = 32;
  value.dtype_lanes = 1;
  value.shape = {2, 3};
  value.strides = {3, 1};
  value.core.byte_size = value.allocation_bytes = 64;
  return value;
}
TEST(GpuDescriptor, ElementStridesAndAlignedWireOffset)
{
  auto value = tensor();
  value.byte_offset = 4;
  EXPECT_EQ(tensor_bytes(value), 24u);
  value.strides = {12, 4};
  EXPECT_THROW(tensor_bytes(value), std::invalid_argument);
}
TEST(GpuDescriptor, EmptyStillValidatesStrideVolume)
{
  auto value = tensor();
  value.shape = {0, INT64_MAX, 2};
  value.strides.clear();
  EXPECT_THROW(tensor_bytes(value), std::invalid_argument);
  value.shape = {0, 3};
  value.byte_offset = 64;
  EXPECT_EQ(tensor_bytes(value), 0u);
}
TEST(GpuDescriptor, RejectsAllocationMismatchAndUnalignedOffset)
{
  auto value = tensor();
  value.core.byte_size = 63;
  EXPECT_THROW(tensor_bytes(value), std::invalid_argument);
  value.core.byte_size = 64;
  value.byte_offset = 1;
  EXPECT_THROW(tensor_bytes(value), std::invalid_argument);
}
TEST(GpuDescriptor, ScalarHasOneElementAndDtypeControlsBounds)
{
  auto value = tensor();
  value.shape.clear();
  value.strides.clear();
  EXPECT_EQ(tensor_bytes(value), 4u);
  value.dtype_bits = 8;
  EXPECT_THROW(tensor_bytes(value), std::invalid_argument);
  value.dtype_code = 1;
  EXPECT_EQ(tensor_bytes(value), 1u);
}
}  // namespace
