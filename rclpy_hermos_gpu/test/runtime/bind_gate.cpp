// Copyright 2026 rclpy zerocopy maintainers
// SPDX-License-Identifier: Apache-2.0
#include <pybind11/pybind11.h>
#include <atomic>
#include <chrono>
#include <thread>
#include "runtime/runtime.hpp"

namespace py = pybind11;
namespace gpu = rclpy_hermos_gpu;
namespace
{
class Gate
{
public:
  explicit Gate(std::uintptr_t stream)
  {
    gpu::check_cuda(cuEventCreate(&complete_, CU_EVENT_DISABLE_TIMING), "gate event create");
    const auto launched = cuLaunchHostFunc(gpu::stream_value(stream), [](void * raw) {
        auto gate = static_cast<Gate *>(raw);
        while (!gate->released_.load()) {
          std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
      }, this);
    if (launched != CUDA_SUCCESS) {
      cuEventDestroy(complete_);
      gpu::check_cuda(launched, "gate launch");
    }
    const auto recorded = cuEventRecord(complete_, gpu::stream_value(stream));
    if (recorded != CUDA_SUCCESS) {
      release();
      cuStreamSynchronize(gpu::stream_value(stream));
      cuEventDestroy(complete_);
      gpu::check_cuda(recorded, "gate event record");
    }
  }
  ~Gate()
  {
    release();
    cuEventSynchronize(complete_);
    cuEventDestroy(complete_);
  }
  void release() {released_.store(true);}
private:
  std::atomic<bool> released_{false};
  CUevent complete_{};
};
}
void bind_test_gate(py::module_ & module)
{
  py::class_<Gate>(module, "_TestGate")
  .def(py::init<std::uintptr_t>())
  .def("release", &Gate::release);
}
