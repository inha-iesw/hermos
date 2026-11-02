cmake_minimum_required(VERSION 3.14)
project(g5_benchmark_msgs)

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/Dynamic.msg"
  "msg/Fixed1K.msg"
  "msg/Fixed4K.msg"
  "msg/Fixed64K.msg"
  "msg/Fixed1M.msg"
  "msg/Fixed4M.msg"
)
ament_export_dependencies(rosidl_default_runtime)
ament_package()
