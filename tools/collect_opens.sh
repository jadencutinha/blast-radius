#!/bin/bash
# Runs sqlglot's tests at the pinned commit and records the data files each one
# reads. Usage: tools/collect_opens.sh <path to a sqlglot checkout at the pin>
# Writes .cache/opens.json.
set -euo pipefail

PIN=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
IMAGE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${1:?path to sqlglot checkout}"

[ "$(git -C "$SRC" rev-parse HEAD)" = "$PIN" ] || { echo "checkout is not at $PIN" >&2; exit 1; }
mkdir -p "$ROOT/.cache"
rm -f "$ROOT/.cache/opens.json"

# Native arm64 for the same reason as collect_coverage.sh.
tar -C "$SRC" --exclude .git -cf - . | docker run --rm -i --platform linux/arm64 \
  --env SKIP_INTEGRATION=0 --env PYTHONPATH=/src:/tools \
  --volume "$ROOT/.cache:/out" --volume "$ROOT/tools:/tools:ro" \
  --volume "$ROOT/requirements.lock:/req.lock:ro" \
  "$IMAGE" sh -c '
    set -e
    mkdir /src && tar -xf - -C /src
    pip install -q --root-user-action=ignore -r /req.lock
    cd /src
    status=0
    python -m pytest tests -q -p inprocess_pool -p trace_opens -p no:cacheprovider --timeout=1800 \
      > /out/opens.log 2>&1 || status=$?
    tail -3 /out/opens.log
    echo "pytest exit $status"
    chmod -R a+rw /out
  '
