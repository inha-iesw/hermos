#!/usr/bin/env bash
set -eo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
unset AMENT_PREFIX_PATH CMAKE_PREFIX_PATH COLCON_PREFIX_PATH PYTHONPATH LD_LIBRARY_PATH LD_PRELOAD
export PYTHONDONTWRITEBYTECODE=1 CMAKE_BUILD_PARALLEL_LEVEL=2
source /opt/ros/humble/setup.bash
source install/hermos/g5/production/local_setup.bash
mkdir -p log/hermos/g5/gpu-comparison
python3 phase4_gpu_core_probe/repair/prepare.py > log/hermos/g5/gpu-comparison/canonical-source-verification.json
colcon --log-base log/hermos/g5/gpu-comparison/core-build build --executor sequential \
  --base-paths build/g3/repaired/ros2_cuda_ipc/ros2_cuda_ipc_msgs \
    build/g3/repaired/ros2_cuda_ipc/ros2_cuda_ipc_core rclpy_hermos_gpu_msgs \
  --packages-select ros2_cuda_ipc_msgs ros2_cuda_ipc_core rclpy_hermos_gpu_msgs \
  --build-base build/hermos/g5/gpu-comparison-core --install-base install/hermos/g5/gpu-comparison-core \
  --cmake-args -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF \
    -DAMENT_CMAKE_SYMLINK_INSTALL=OFF -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
    -DPython3_EXECUTABLE=/usr/bin/python3 -DCUDAToolkit_ROOT=/usr/local/cuda-13.2
test -f install/hermos/g5/gpu-comparison-core/ros2_cuda_ipc_core/lib/libros2_cuda_ipc_core.a
for g5_message_package in ros2_cuda_ipc_msgs rclpy_hermos_gpu_msgs; do
  test -f "install/hermos/g5/gpu-comparison-core/$g5_message_package/lib/lib${g5_message_package}__rosidl_typesupport_fastrtps_c.so"
  test -f "install/hermos/g5/gpu-comparison-core/$g5_message_package/lib/lib${g5_message_package}__rosidl_typesupport_fastrtps_cpp.so"
done
source install/hermos/g5/gpu-comparison-core/local_setup.bash
colcon --log-base log/hermos/g5/gpu-comparison/adapter-build build --executor sequential \
  --base-paths rclpy_hermos_gpu --packages-select rclpy_hermos_gpu \
  --build-base build/hermos/g5/gpu-comparison --install-base install/hermos/g5/gpu-comparison \
  --cmake-args -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF \
    -DAMENT_CMAKE_SYMLINK_INSTALL=OFF -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
    -DPython3_EXECUTABLE=/usr/bin/python3 -DCUDAToolkit_ROOT=/usr/local/cuda-13.2
