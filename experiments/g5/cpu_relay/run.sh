#!/usr/bin/env bash
# Reuse the production CPU artifacts and the canonical owned device session.
set -eo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
g5_relay_stage="${1:?usage: run.sh /absolute/new-stage [--smoke]}"
shift
source experiments/g5/environment.sh
exec python3 phase4_gpu_core_probe/repair/session/run_device.py "$g5_relay_stage" \
  python3 -m experiments.g5.cpu_relay.run --output "$g5_relay_stage/results" "$@"
