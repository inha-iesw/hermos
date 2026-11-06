cmake_minimum_required(VERSION 3.20)
project(hermos_runtime)

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(Python3 REQUIRED COMPONENTS Interpreter)
find_package(hermoslib REQUIRED)
find_package(rclpy_hermos REQUIRED)

ament_python_install_package(${PROJECT_NAME})
ament_get_python_install_dir(python_install_dir)
file(GENERATE OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/supervision.json"
  CONTENT "{\"version\": 1, \"python_dir\": \"${python_install_dir}\"}\n")
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/supervision.json" DESTINATION share/${PROJECT_NAME})
install(FILES scripts/namespace_worker.py DESTINATION lib/${PROJECT_NAME})
install(PROGRAMS scripts/hermos_supervisor scripts/hermos_cpu_peer scripts/hermos_metadata_peer
  DESTINATION lib/${PROJECT_NAME})
install(DIRECTORY include/ DESTINATION include)
ament_export_include_directories(include)
get_filename_component(backend_prefix "${hermoslib_DIR}/../../.." ABSOLUTE)
get_filename_component(native_prefix "${rclpy_hermos_DIR}/../../.." ABSOLUTE)

add_executable(cpu_install_abi src/environment/install_abi.cpp)
target_link_libraries(cpu_install_abi PRIVATE rclpy_hermos::rclpy_hermos_types)
target_compile_features(cpu_install_abi PRIVATE cxx_std_17)
add_custom_target(cpu_install_manifest ALL
  COMMAND cpu_install_abi "${CMAKE_CURRENT_BINARY_DIR}/abi.txt"
  COMMAND ${Python3_EXECUTABLE}
    "${CMAKE_CURRENT_SOURCE_DIR}/hermos_runtime/environment/build_manifest.py"
    --backend-prefix "${backend_prefix}" --native-prefix "${native_prefix}"
    --python-dir "${python_install_dir}" --abi "${CMAKE_CURRENT_BINARY_DIR}/abi.txt"
    --source-root "${CMAKE_CURRENT_SOURCE_DIR}/.."
    --output "${CMAKE_CURRENT_BINARY_DIR}/cpu-manifest.json"
  DEPENDS cpu_install_abi
  VERBATIM)
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/cpu-manifest.json" DESTINATION share/${PROJECT_NAME})
install(DIRECTORY cmake/ DESTINATION share/${PROJECT_NAME}/cmake)
install(DIRECTORY packaging/system/ DESTINATION share/${PROJECT_NAME}/system)
install(FILES README.md DESTINATION share/${PROJECT_NAME})

if(BUILD_TESTING)
  find_package(ament_cmake_pytest REQUIRED)
  add_executable(supervision_registration_peer test/supervision/registration_peer.cpp)
  target_compile_features(supervision_registration_peer PRIVATE cxx_std_17)
  target_include_directories(supervision_registration_peer PRIVATE include)
  ament_add_pytest_test(test_cpu_install_manifest test/environment TIMEOUT 60)
  ament_add_pytest_test(test_cpu_bootstrap test/cpu TIMEOUT 60)
  ament_add_pytest_test(test_kernel_install_contract test/kernel TIMEOUT 60)
  ament_add_pytest_test(test_supervision test/supervision TIMEOUT 120
    ENV "HERMOS_RUNTIME_TEST_PREFIX=${CMAKE_INSTALL_PREFIX}"
        "HERMOS_REGISTRATION_PEER=$<TARGET_FILE:supervision_registration_peer>")
  ament_add_pytest_test(test_observation test/observation TIMEOUT 240)
endif()
ament_package(CONFIG_EXTRAS cmake/hermos_runtime-extras.cmake)
