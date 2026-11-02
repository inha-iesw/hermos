// Copyright 2026 rclpy zerocopy maintainers
// SPDX-License-Identifier: Apache-2.0

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>

#include "rclpy_hermos/types/adapter.hpp"
#include "rclpy_hermos/types/field_ops.hpp"

namespace
{
using rclpy_hermos::types::ADAPTER_ABI_VERSION;
using rclpy_hermos::types::compiled_abi_tag;
using rclpy_hermos::types::message_ops;
using rclpy_hermos::types::NativeEndpoint;
using rclpy_hermos::types::ProviderDescriptor;
using rclpy_hermos::types::Status;
using rclpy_hermos::types::status_from_exception;
using rclpy_hermos::types::TypeOps;

Status fail_creation(void *, const char *, std::size_t, NativeEndpoint * endpoint) noexcept
{
  *endpoint = {};
  try {
    throw std::runtime_error("provider-local factory failure");
  } catch (...) {
    return status_from_exception();
  }
}

Status unused_subscription(
  void *, const char *, std::size_t, void *, void *,
  rclpy_hermos::types::NativeReceive,
  rclpy_hermos::types::NativeSubscriptionEndpoint *) noexcept
{return {rclpy_hermos::types::ErrorCode::kUnsupported, "fixture has no subscription"};}

const auto root = message_ops<std::int64_t>(nullptr, 0);
const TypeOps operations{&root, fail_creation, unused_subscription};
const ProviderDescriptor provider{
  ADAPTER_ABI_VERSION, "lifetime_test_provider", "lifetime_test_provider._plugin",
  compiled_abi_tag(), std::string(64, 'a'),
  {{"lifetime_test/msg/Root", std::string(64, 'b'), "[]", sizeof(std::int64_t),
    alignof(std::int64_t), &operations}}
};
}  // namespace

/**
 * @brief Expose the unloadable test provider without registering it implicitly.
 * @return Borrowed descriptor valid only while this library is loaded or process-pinned.
 */
extern "C" __attribute__((visibility("default")))
const rclpy_hermos::types::ProviderDescriptor * test_provider_descriptor()
{
  return &provider;
}
