#!/usr/bin/env bash
# Build the image and run the full pipeline; any arguments are passed to run_experiments.py.
set -euo pipefail
cd "$(dirname "$0")"

IMAGE=cs422-a2
docker build -t "$IMAGE" .
mkdir -p results
docker run --rm --network host --cap-add NET_ADMIN \
    -e GIT_SHA="$(git rev-parse HEAD 2>/dev/null || echo unknown)" \
    -v "$PWD/results:/app/results" \
    "$IMAGE" "$@"
