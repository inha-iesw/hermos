// Copyright 2026 rclpy zerocopy maintainers
// SPDX-License-Identifier: Apache-2.0
// Test-owned opt-in fault after real CUDA event creation/recording.
#include <cuda.h>
#include <dlfcn.h>
#include <unistd.h>
#include <atomic>
#include <cstdlib>

extern "C" CUresult cuEventQuery(CUevent event)
{
  static const char * marker = std::getenv("G4_QUERY_FAILURE_MARKER");
  static std::atomic<bool> failed{false};
  if (marker && access(marker, F_OK) == 0 && !failed.exchange(true)) {
    return CUDA_ERROR_UNKNOWN;
  }
  static const auto original = []() {
      auto address = dlsym(RTLD_NEXT, "cuEventQuery");
      if (!address) {
        // Torch may load libcuda RTLD_LOCAL; RTLD_NEXT alone then cannot see it.
        auto driver = dlopen("libcuda.so.1", RTLD_NOW | RTLD_LOCAL);
        if (driver) {address = dlsym(driver, "cuEventQuery");}
      }
      return reinterpret_cast<decltype(&cuEventQuery)>(address);
    }();
  return original ? original(event) : CUDA_ERROR_UNKNOWN;
}
