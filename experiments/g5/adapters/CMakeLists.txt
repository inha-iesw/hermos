cmake_minimum_required(VERSION 3.14)
project(g5_benchmark_adapters)

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(rclpy_hermos_codegen REQUIRED)
ament_python_install_package(${PROJECT_NAME})
rclpy_hermos_generate_adapters(_types TYPES
  g5_benchmark_msgs/msg/Dynamic
  g5_benchmark_msgs/msg/Fixed1K
  g5_benchmark_msgs/msg/Fixed4K
  g5_benchmark_msgs/msg/Fixed64K
  g5_benchmark_msgs/msg/Fixed1M
  g5_benchmark_msgs/msg/Fixed4M
)
if(BUILD_TESTING)
  find_package(ament_cmake_pytest REQUIRED)
  ament_add_pytest_test(test_cpu_latency ../cpu_latency/test
    APPEND_ENV "PYTHONPATH=${CMAKE_CURRENT_SOURCE_DIR}/../../.."
    TIMEOUT 60)
  ament_add_pytest_test(test_cpu_relay ../cpu_relay/test
    APPEND_ENV "PYTHONPATH=${CMAKE_CURRENT_SOURCE_DIR}/../../.."
    TIMEOUT 60)
  ament_add_pytest_test(test_g5_harness ../test
    APPEND_ENV "PYTHONPATH=${CMAKE_CURRENT_SOURCE_DIR}/../../.."
    TIMEOUT 60)
  ament_add_pytest_test(test_g5_evaluation ../evaluation/test
    APPEND_ENV "PYTHONPATH=${CMAKE_CURRENT_SOURCE_DIR}/../../.."
    TIMEOUT 60)
  ament_add_pytest_test(test_gpu_matrix ../gpu_latency/test/test_gpu_matrix.py
    APPEND_ENV "PYTHONPATH=${CMAKE_CURRENT_SOURCE_DIR}/../../.."
    TIMEOUT 60)
  ament_add_pytest_test(test_gpu_fanout ../gpu_fanout/test
    APPEND_ENV "PYTHONPATH=${CMAKE_CURRENT_SOURCE_DIR}/../../.."
    TIMEOUT 60)
endif()
ament_package()
