cmake_minimum_required(VERSION 3.14)
project(rclpy_hermos_adapters)

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(rclpy_hermos_codegen REQUIRED)

ament_python_install_package(${PROJECT_NAME})
rclpy_hermos_generate_adapters(_types TYPES
  sensor_msgs/msg/Image
  sensor_msgs/msg/PointCloud2
)

if(BUILD_TESTING)
  find_package(ament_cmake_pytest REQUIRED)
  ament_add_pytest_test(test_installed_provider test/test_installed_provider.py TIMEOUT 60)
  ament_add_pytest_test(test_build_contract test/test_build_contract.py TIMEOUT 180)
endif()
ament_package()
