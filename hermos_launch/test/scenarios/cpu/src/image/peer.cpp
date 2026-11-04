// SPDX-License-Identifier: Apache-2.0
#include <dlfcn.h>
#include <unistd.h>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdlib>
#include <exception>
#include <iomanip>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>

#include "hermos/hermos.hpp"
#include "sensor_msgs/msg/image.hpp"

namespace
{
volatile std::sig_atomic_t stop_signal = 0;

void request_stop(int value) {stop_signal = value;}

void ready(const std::shared_ptr<hermos::Node> & node, const std::string & role,
  const std::string & label, int argc, char ** argv)
{
  Dl_info allocator{};
  Dl_info backend{};
  dladdr(dlsym(RTLD_DEFAULT, "malloc"), &allocator);
  dladdr(dlsym(RTLD_DEFAULT, "initialize_hermos"), &backend);
  std::cout << "HERMOS_EXAMPLE {\"event\":\"ready\",\"role\":" << std::quoted("cpp_" + role)
            << ",\"pid\":" << getpid() << ",\"name\":" << std::quoted(node->get_name())
            << ",\"namespace\":" << std::quoted(node->get_namespace())
            << ",\"label\":" << std::quoted(label)
            << ",\"malloc_provider\":" << std::quoted(allocator.dli_fname ? allocator.dli_fname : "")
            << ",\"loaded\":[" << std::quoted(backend.dli_fname ? backend.dli_fname : "")
            << "],\"argv\":[";
  for (int index = 0; index < argc; ++index) {
    if (index) {std::cout << ',';}
    std::cout << std::quoted(argv[index]);
  }
  std::cout << "]}" << std::endl;
}

void run_images(int argc, char ** argv)
{
  rclcpp::NodeOptions options;
  options.start_parameter_services(false);
  options.start_parameter_event_publisher(false);
  options.use_clock_thread(false);
  auto node = std::make_shared<hermos::Node>("image_cpp_peer", options);
  const auto expected = node->declare_parameter<int64_t>("count", 6);
  const auto label = node->declare_parameter<std::string>("label", "CPU example label");
  const auto role = node->declare_parameter<std::string>("role", "subscriber");
  if (expected < 1) {throw std::invalid_argument("count must be positive");}
  if (role != "publisher" && role != "subscriber") {
    throw std::invalid_argument("role must be publisher or subscriber");
  }
  std::atomic<int64_t> count{0};
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(15);
  if (role == "publisher") {
    auto publisher = node->create_publisher<sensor_msgs::msg::Image>(
      "image", rclcpp::QoS(8).best_effort());
    ready(node, role, label, argc, argv);
    while (count < expected && !stop_signal && std::chrono::steady_clock::now() < deadline) {
      if (node->count_subscribers("image") > 0) {
        auto message = publisher->borrow_loaned_message();
        message->header.stamp.sec = static_cast<int32_t>(count.load());
        message->width = 64;
        message->height = 1;
        message->step = 64;
        message->encoding = "mono8";
        message->is_bigendian = 0;
        message->data.resize(64);
        for (std::size_t offset = 0; offset < message->data.size(); ++offset) {
          message->data[offset] = static_cast<uint8_t>((count.load() + offset) % 256);
        }
        publisher->publish(std::move(message));
        ++count;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    if (count != expected && !stop_signal) {throw std::runtime_error("Image publish timeout");}
    std::cout << "HERMOS_EXAMPLE {\"event\":\"done\",\"role\":\"cpp_publisher\",\"count\":"
              << count << ",\"expected\":" << expected << ",\"signal\":" << stop_signal
              << "}" << std::endl;
    return;
  }
  std::atomic<bool> failed{false};
  auto subscription = node->create_subscription<sensor_msgs::msg::Image>(
    "image", rclcpp::QoS(8).best_effort(),
    [&count](const hermos::ipc_shared_ptr<const sensor_msgs::msg::Image> & message) {
      const auto index = count.load();
      if (message->header.stamp.sec != index || message->width != 64 ||
        message->height != 1 || message->step != 64 || message->encoding != "mono8" ||
        message->data.size() != 64)
      {
        throw std::runtime_error("Image metadata or sequence mismatch");
      }
      for (std::size_t offset = 0; offset < message->data.size(); ++offset) {
        if (message->data[offset] != static_cast<uint8_t>((index + offset) % 256)) {
          throw std::runtime_error("Image payload mismatch");
        }
      }
      ++count;
    });
  hermos::HermosOnlySingleThreadedExecutor executor;
  executor.add_node(node);
  std::exception_ptr failure;
  std::thread worker([&] {
      try {executor.spin();} catch (...) {
        failure = std::current_exception();
        failed = true;
      }
    });
  ready(node, role, label, argc, argv);
  while (count < expected && !failed && !stop_signal &&
    std::chrono::steady_clock::now() < deadline)
  {
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
  executor.cancel();
  worker.join();
  executor.remove_node(node);
  if (failure) {std::rethrow_exception(failure);}
  if (count != expected && !stop_signal) {throw std::runtime_error("Image receive timeout");}
  std::cout << "HERMOS_EXAMPLE {\"event\":\"done\",\"role\":\"cpp_subscriber\",\"count\":"
            << count << ",\"expected\":" << expected << ",\"signal\":" << stop_signal
            << "}" << std::endl;
}
}  // namespace

int main(int argc, char ** argv)
{
  try {
    hermos::init(argc, argv);
    std::signal(SIGINT, request_stop);
    std::signal(SIGTERM, request_stop);
    run_images(argc, argv);
    hermos::shutdown();
    std::cout << "HERMOS_EXAMPLE {\"event\":\"closed\",\"role\":\"cpp_peer\",\"pid\":"
              << getpid() << "}" << std::endl;
    return 0;
  } catch (const std::exception & error) {
    std::cerr << "CPU Image reader: " << error.what() << std::endl;
    hermos::shutdown();
    return 1;
  }
}
