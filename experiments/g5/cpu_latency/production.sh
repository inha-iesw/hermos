#!/usr/bin/env bash
# Fresh performance prefix; never overwrite the verified G2 device artifacts.
set -eo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
g5_workspace="$PWD"
unset AMENT_PREFIX_PATH CMAKE_PREFIX_PATH COLCON_PREFIX_PATH PYTHONPATH LD_LIBRARY_PATH LD_PRELOAD
export PYTHONDONTWRITEBYTECODE=1 CMAKE_BUILD_PARALLEL_LEVEL=2
source /opt/ros/humble/setup.bash
colcon --log-base log/hermos/g5/production-build build --executor sequential \
  --base-paths hermos rclpy_zc rclpy_zc_msgs rclpy_hermos_codegen \
    experiments/g5/messages experiments/g5/adapters \
  --packages-up-to g5_benchmark_adapters \
  --build-base build/hermos/g5/production --install-base install/hermos/g5/production \
  --cmake-args -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF \
    -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -DPython3_EXECUTABLE=/usr/bin/python3
(
  cd hermos/hermos_heaphook
  cargo build --locked --offline --release --no-default-features \
    --target-dir "$g5_workspace/build/hermos/g5/heaphook"
)
cp build/hermos/g5/heaphook/release/libhermos_heaphook.so \
  install/hermos/g5/production/hermoslib/lib/libhermos_heaphook.so
