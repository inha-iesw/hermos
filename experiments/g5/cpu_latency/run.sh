#!/usr/bin/env bash
# Keep the parent free of heaphook preload and old Debug overlays.
set -eo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
g5_stage="${1:?usage: run.sh /absolute/new-stage [--smoke] [--rate-diagnostic]}"
shift
source experiments/g5/environment.sh
exec python3 phase4_gpu_core_probe/repair/session/run_device.py "$g5_stage" \
  python3 -m experiments.g5.cpu_latency.run --output "$g5_stage/results" "$@"
