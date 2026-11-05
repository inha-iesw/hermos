cmake_minimum_required(VERSION 3.14)
project(hermos_gpu_examples)

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)

ament_python_install_package(${PROJECT_NAME})
install(PROGRAMS scripts/dds_control DESTINATION lib/${PROJECT_NAME})
install(DIRECTORY launch/ DESTINATION share/${PROJECT_NAME}/launch)
install(DIRECTORY config/ DESTINATION share/${PROJECT_NAME}/config)

if(BUILD_TESTING)
  find_package(ament_cmake_pytest REQUIRED)
  ament_add_pytest_test(test_gpu_pipeline_proof test/test_proof.py TIMEOUT 30)
  ament_add_pytest_test(test_gpu_pipeline_installation test/test_installed.py TIMEOUT 840)
  set_tests_properties(test_gpu_pipeline_installation PROPERTIES
    LABELS "pytest;system;requires_kernel_module;requires_heaphook;requires_cuda")
endif()
ament_package()
