ament_add_pytest_test(test_namespace_device recovery/test_namespace_device.py TIMEOUT 180
  ENV G3_TRANSPORT_PEER=$<TARGET_FILE:g3_transport_peer>
    G3_CLEANUP_PEER=$<TARGET_FILE:g3_cleanup_peer>)
set_tests_properties(test_namespace_device PROPERTIES LABELS "requires_kernel_module;cuda")
