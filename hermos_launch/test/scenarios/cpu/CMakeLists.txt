cmake_minimum_required(VERSION 3.14)
project(hermos_cpu_examples)

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(hermos_runtime REQUIRED)
find_package(hermoslib REQUIRED)
find_package(sensor_msgs REQUIRED)

ament_python_install_package(${PROJECT_NAME})
hermos_install_cpu_executable(image_peer
  PYTHON_ENTRY hermos_cpu_examples.image.peer:main PROVIDERS rclpy_hermos_adapters)
add_executable(native_image_peer src/image/peer.cpp)
target_compile_features(native_image_peer PRIVATE cxx_std_17)
target_link_libraries(native_image_peer hermoslib::hermos ${CMAKE_DL_LIBS})
ament_target_dependencies(native_image_peer sensor_msgs)
hermos_install_cpu_executable(cpp_image_reader
  TARGET native_image_peer PROVIDERS rclpy_hermos_adapters)
hermos_install_cpu_executable(cpp_image_peer
  TARGET native_image_peer PROVIDERS rclpy_hermos_adapters)
install(DIRECTORY launch/ DESTINATION share/${PROJECT_NAME}/launch)
install(DIRECTORY config/ DESTINATION share/${PROJECT_NAME}/config)

if(BUILD_TESTING)
  find_package(ament_cmake_pytest REQUIRED)
  ament_add_pytest_test(test_cpu_example_installation test/test_discovery.py TIMEOUT 30)
  ament_add_pytest_test(test_cpu_installed_examples test/test_installed.py TIMEOUT 180)
  set_tests_properties(test_cpu_installed_examples PROPERTIES
    LABELS "pytest;system;requires_kernel_module;requires_heaphook")
endif()
ament_package()
