#!/usr/bin/env bash
# Source from the workspace root; every G5 driver starts from the same ROS overlay.
unset AMENT_PREFIX_PATH CMAKE_PREFIX_PATH COLCON_PREFIX_PATH PYTHONPATH LD_LIBRARY_PATH LD_PRELOAD
unset CYCLONEDDS_URI FASTRTPS_DEFAULT_PROFILES_FILE FASTDDS_DEFAULT_PROFILES_FILE
unset RMW_FASTRTPS_USE_QOS_FROM_XML RMW_FASTRTPS_PUBLICATION_MODE
export SKIP_DEFAULT_XML=1 PYTHONDONTWRITEBYTECODE=1
source /opt/ros/humble/setup.bash
source install/hermos/g5/production/local_setup.bash
export PYTHONPATH="$PWD:${PYTHONPATH:-}"
