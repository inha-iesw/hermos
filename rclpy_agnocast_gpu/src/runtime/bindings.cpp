// Copyright 2026 rclpy zerocopy maintainers
// SPDX-License-Identifier: Apache-2.0
#include "runtime.hpp"
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <limits>

namespace py = pybind11;
namespace gpu = rclpy_agnocast_gpu;
#ifdef BUILD_TESTING
void bind_test_gate(py::module_ & module);
#endif
namespace
{
py::dict as_dict(const gpu::Message & message)
{
  py::dict core;
  core["vmm_socket_path"] = message.core.vmm_socket_path;
  core["event_handle"] = message.core.event_handle;
  core["publisher_pid"] = message.core.publisher_pid;
  core["device_id"] = message.core.device_id;
  core["block_id"] = message.core.block_id;
  core["uid"] = message.core.uid;
  core["byte_size"] = message.core.byte_size;
  py::dict result;
  result["core"] = core;
  result["dtype_code"] = message.dtype_code;
  result["dtype_bits"] = message.dtype_bits;
  result["dtype_lanes"] = message.dtype_lanes;
  result["shape"] = message.shape;
  result["strides"] = message.strides;
  result["byte_offset"] = message.byte_offset;
  result["allocation_bytes"] = message.allocation_bytes;
  return result;
}
template<typename T>
T integer(py::handle value)
{
  if (PyBool_Check(value.ptr()) || !PyLong_Check(value.ptr())) {
    throw py::type_error("GPU metadata integers must be int, not bool");
  }
  return py::cast<T>(value);
}
gpu::Message from_dict(py::dict input)
{
  gpu::Message message;
  auto core = py::cast<py::dict>(input["core"]);
  message.core.vmm_socket_path = py::cast<std::string>(core["vmm_socket_path"]);
  auto handles = py::cast<py::sequence>(core["event_handle"]);
  if (handles.size() != 64) {throw py::value_error("CUDA event handle must contain 64 bytes");}
  for (std::size_t i = 0; i < 64; ++i) {
    message.core.event_handle[i] = integer<std::uint8_t>(handles[i]);
  }
  message.core.publisher_pid = integer<std::uint32_t>(core["publisher_pid"]);
  message.core.device_id = integer<std::uint32_t>(core["device_id"]);
  message.core.block_id = integer<std::uint32_t>(core["block_id"]);
  message.core.uid = integer<std::uint64_t>(core["uid"]);
  message.core.byte_size = integer<std::uint64_t>(core["byte_size"]);
  message.dtype_code = integer<std::uint8_t>(input["dtype_code"]);
  message.dtype_bits = integer<std::uint8_t>(input["dtype_bits"]);
  message.dtype_lanes = integer<std::uint16_t>(input["dtype_lanes"]);
  for (auto value : py::cast<py::iterable>(input["shape"])) {
    message.shape.push_back(integer<std::int64_t>(value));
  }
  for (auto value : py::cast<py::iterable>(input["strides"])) {
    message.strides.push_back(integer<std::int64_t>(value));
  }
  message.byte_offset = integer<std::uint64_t>(input["byte_offset"]);
  message.allocation_bytes = integer<std::uint64_t>(input["allocation_bytes"]);
  return message;
}
py::capsule capsule(DLManagedTensor * managed)
{
  auto * value = PyCapsule_New(managed, "dltensor", [](PyObject * object) {
      const bool unconsumed = PyCapsule_IsValid(object, "dltensor");
      const char * name = unconsumed ? "dltensor" : "used_dltensor";
      if (PyCapsule_IsValid(object, name)) {
        gpu::release_capsule(
          static_cast<DLManagedTensor *>(PyCapsule_GetPointer(object, name)), unconsumed);
      }
    });
  if (!value) {managed->deleter(managed); throw py::error_already_set();}
  gpu::retain_capsule(managed);
  return py::reinterpret_steal<py::capsule>(value);
}
}
PYBIND11_MODULE(_native, module)
{
#ifdef BUILD_TESTING
  bind_test_gate(module);
#endif
  module.doc() = "Single-process CUDA core, stream-bound Tensor ownership and drain.";
  py::class_<gpu::Runtime, std::shared_ptr<gpu::Runtime>>(module, "Runtime")
  .def(py::init([](py::handle blocks, py::handle bytes, py::handle device,
      py::handle reads, py::handle maximum) {
      return std::make_shared<gpu::Runtime>(integer<std::size_t>(blocks),
        integer<std::uint64_t>(bytes), integer<int>(device), integer<std::size_t>(reads),
        integer<std::uint64_t>(maximum));
    }))
  .def("initialise", &gpu::Runtime::initialise, py::call_guard<py::gil_scoped_release>())
  .def("owned_ipc_paths", &gpu::Runtime::owned_ipc_paths)
  .def("borrow", [](gpu::Runtime & runtime, py::iterable shape, py::handle code,
      py::handle bits, py::handle stream, py::handle owner) {
      std::vector<std::int64_t> dimensions;
      for (auto value : shape) {dimensions.push_back(integer<std::int64_t>(value));}
      const auto c = integer<unsigned>(code), b = integer<unsigned>(bits);
      const auto s = integer<std::uintptr_t>(stream), o = integer<std::uint64_t>(owner);
      py::gil_scoped_release release;
      return runtime.borrow(std::move(dimensions), c, b, s, o);
    })
  .def("map", [](gpu::Runtime & runtime, py::dict descriptor,
      std::uintptr_t stream, std::uint64_t owner) {
      auto message = from_dict(descriptor);
      py::gil_scoped_release release;
      return runtime.map(std::move(message), stream, owner);
    })
  .def("poll", &gpu::Runtime::poll, py::call_guard<py::gil_scoped_release>())
  .def("seal", &gpu::Runtime::seal, py::call_guard<py::gil_scoped_release>())
  .def("owner_active", &gpu::Runtime::owner_active, py::call_guard<py::gil_scoped_release>())
  .def("try_close", &gpu::Runtime::try_close, py::call_guard<py::gil_scoped_release>())
  .def_property_readonly("poisoned", &gpu::Runtime::poisoned)
  .def_property_readonly("closing", &gpu::Runtime::closing)
  .def_property_readonly("stats", [](gpu::Runtime & runtime) {
      const auto value = runtime.stats();
      py::dict result;
      result["active_owners"] = value.owners;
      result["pending_owners"] = value.pending;
      result["peak_owners"] = value.peak_owners;
      result["wrapper_events"] = value.wrapper_events;
      result["configured_pool_bytes"] = value.pool_bytes;
      return result;
    });
  py::class_<gpu::Loan, std::shared_ptr<gpu::Loan>>(module, "Loan")
  .def("dlpack", [](gpu::Loan & loan, std::uintptr_t stream) {return capsule(loan.dlpack(stream));})
  .def("prepare", [](gpu::Loan & loan) {
      gpu::Message message;
      {py::gil_scoped_release release; message = loan.prepare();}
      return as_dict(message);
    })
  .def("cancel", &gpu::Loan::cancel)
  .def_property_readonly("state", &gpu::Loan::state)
  .def_property_readonly("pointer", &gpu::Loan::pointer)
  .def_property_readonly("exports", &gpu::Loan::exports);
  py::class_<gpu::ReadExport>(module, "ReadExport")
  .def("dlpack", [](gpu::ReadExport & read, std::uintptr_t stream) {return capsule(read.dlpack(stream));})
  .def_property_readonly("pointer", &gpu::ReadExport::pointer);
  module.def("discard_dlpack", [](py::handle object) {
      if (PyCapsule_IsValid(object.ptr(), "dltensor")) {
        auto * managed = static_cast<DLManagedTensor *>(PyCapsule_GetPointer(object.ptr(), "dltensor"));
        if (PyCapsule_SetName(object.ptr(), "used_dltensor") != 0) {throw py::error_already_set();}
        managed->deleter(managed);
      }
    });
  module.def("validate_descriptor", [](py::dict value) {
      return gpu::descriptor::tensor_bytes(from_dict(value));
    });
}
