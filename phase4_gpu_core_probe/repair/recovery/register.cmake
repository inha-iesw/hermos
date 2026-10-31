ament_add_pytest_test(test_peer_recovery recovery/test_group.py TIMEOUT 60)
ament_add_pytest_test(test_isolated_epoch recovery/test_isolated.py TIMEOUT 60)
ament_add_pytest_test(test_namespace_worker recovery/test_namespace_worker.py TIMEOUT 60)
ament_add_pytest_test(test_namespace_bootstrap recovery/test_bootstrap.py TIMEOUT 60)
ament_add_pytest_test(test_namespace_device recovery/test_namespace_device.py TIMEOUT 180
  ENV G3_TRANSPORT_PEER=$<TARGET_FILE:g3_transport_peer>
    G3_CLEANUP_PEER=$<TARGET_FILE:g3_cleanup_peer>)
set_tests_properties(test_namespace_device PROPERTIES LABELS "requires_kernel_module;cuda")
