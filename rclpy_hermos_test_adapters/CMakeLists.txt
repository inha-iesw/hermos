cmake_minimum_required(VERSION 3.14)
project(rclpy_hermos_test_adapters)

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(rclpy_hermos_codegen REQUIRED)

ament_python_install_package(${PROJECT_NAME})
rclpy_hermos_generate_adapters(_types TYPES
  rclpy_hermos_test_msgs/msg/Scalars
  rclpy_hermos_test_msgs/msg/Containers
  rclpy_hermos_test_msgs/msg/Nested
)

if(BUILD_TESTING)
  find_package(ament_cmake_pytest REQUIRED)
  ament_add_pytest_test(test_installed_provider test/test_installed_provider.py TIMEOUT 60)
  find_package(Threads REQUIRED)
  add_executable(rclpy_hermos_typed_inspector test/typed/inspector.cpp)
  target_compile_features(rclpy_hermos_typed_inspector PRIVATE cxx_std_17)
  target_link_libraries(rclpy_hermos_typed_inspector PRIVATE
    hermoslib::hermos rclcpp::rclcpp Threads::Threads
    rclpy_hermos_test_msgs::rclpy_hermos_test_msgs__rosidl_generator_cpp)
  ament_add_pytest_test(test_typed_system test/typed/test_system.py
    ENV "RCLPY_HERMOS_TYPED_INSPECTOR=$<TARGET_FILE:rclpy_hermos_typed_inspector>"
    APPEND_ENV "PYTHONPATH=${CMAKE_CURRENT_SOURCE_DIR}/../rclpy_zc/test"
    TIMEOUT 180)
  set_tests_properties(test_typed_system PROPERTIES
    LABELS "pytest;system;requires_kernel_module;requires_heaphook")

  find_package(sensor_msgs REQUIRED)
  find_package(pybind11_vendor REQUIRED)
  find_package(pybind11 CONFIG REQUIRED)
  add_executable(rclpy_hermos_typed_facade_native test/typed/facade_native.cpp)
  target_compile_features(rclpy_hermos_typed_facade_native PRIVATE cxx_std_17)
  target_link_libraries(rclpy_hermos_typed_facade_native PRIVATE
    hermoslib::hermos rclcpp::rclcpp Threads::Threads
    sensor_msgs::sensor_msgs__rosidl_generator_cpp
    rclpy_hermos_test_msgs::rclpy_hermos_test_msgs__rosidl_generator_cpp)
  pybind11_add_module(_typed_oracle MODULE test/typed/oracle.cpp)
  target_compile_features(_typed_oracle PRIVATE cxx_std_17)
  target_link_libraries(_typed_oracle PRIVATE
    sensor_msgs::sensor_msgs__rosidl_generator_cpp
    rclpy_hermos_test_msgs::rclpy_hermos_test_msgs__rosidl_generator_cpp)
  ament_add_pytest_test(test_typed_facade_system test/typed/test_facade_system.py
    ENV "RCLPY_HERMOS_TYPED_FACADE_NATIVE=$<TARGET_FILE:rclpy_hermos_typed_facade_native>"
    APPEND_ENV
      "PYTHONPATH=${CMAKE_CURRENT_SOURCE_DIR}/../rclpy_zc/test:$<TARGET_FILE_DIR:_typed_oracle>"
    TIMEOUT 240)
  set_tests_properties(test_typed_facade_system PROPERTIES
    LABELS "pytest;system;requires_kernel_module;requires_heaphook")
  ament_add_pytest_test(test_typed_facade_lifecycle test/typed/test_facade_lifecycle.py
    APPEND_ENV
      "PYTHONPATH=${CMAKE_CURRENT_SOURCE_DIR}/../rclpy_zc/test:$<TARGET_FILE_DIR:_typed_oracle>"
    TIMEOUT 300)
  set_tests_properties(test_typed_facade_lifecycle PROPERTIES
    LABELS "pytest;system;requires_kernel_module;requires_heaphook")
endif()
ament_package()
