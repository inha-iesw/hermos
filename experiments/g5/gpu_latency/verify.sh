#!/usr/bin/env bash
# Explicit GPU-artifact host gate: imports production bindings without CUDA work.
set -eo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
source experiments/g5/environment.sh
source install/hermos/g5/gpu-comparison-core/local_setup.bash
source install/hermos/g5/gpu-comparison/local_setup.bash
exec python3 -m pytest -q experiments/g5/gpu_latency/test \
  experiments/g5/gpu_latency/costs/test "$@"
