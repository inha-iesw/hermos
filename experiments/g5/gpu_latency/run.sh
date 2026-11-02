#!/usr/bin/env bash
set -eo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
g5_stage="${1:?usage: run.sh /absolute/new-stage [--smoke] [--compare-dds]}"
shift
source experiments/g5/environment.sh
source install/hermos/g5/gpu-comparison-core/local_setup.bash
source install/hermos/g5/gpu-comparison/local_setup.bash
exec python3 phase4_gpu_core_probe/repair/session/run_device.py "$g5_stage" \
  python3 -m experiments.g5.gpu_latency.run --output "$g5_stage/results" "$@"
