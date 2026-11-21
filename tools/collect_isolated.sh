#!/bin/bash
# Measures each sqlglot test alone at the pinned commit.
# Usage: tools/collect_isolated.sh <sqlglot checkout at the pin> [number of tests]
# Writes .cache/isolated.json, or .cache/isolated_smoke.json with a test limit.
set -euo pipefail

PIN=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
IMAGE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${1:?path to sqlglot checkout}"
LIMIT="${2:-0}"
RESULT=/out/isolated.json
[ "$LIMIT" = 0 ] || RESULT=/out/isolated_smoke.json

[ "$(git -C "$SRC" rev-parse HEAD)" = "$PIN" ] || { echo "checkout is not at $PIN" >&2; exit 1; }
mkdir -p "$ROOT/.cache"

# Native arm64 because duckdb crashes under this VM's amd64 emulation.
tar -C "$SRC" --exclude .git -cf - . | docker run --rm -i --platform linux/arm64 \
  --env SKIP_INTEGRATION=0 --env PYTHONPATH=/src:/tools \
  --env ISOLATED_LIMIT="$LIMIT" --env ISOLATED_RESULT="$RESULT" \
  --volume "$ROOT/.cache:/out" --volume "$ROOT/tools:/tools:ro" \
  --volume "$ROOT/sample/tests.json:/tests.json:ro" \
  --volume "$ROOT/requirements.lock:/req.lock:ro" \
  "$IMAGE" sh -c '
    set -e
    mkdir /src && tar -xf - -C /src
    pip install -q --root-user-action=ignore -r /req.lock coverage==7.13.0
    python /tools/collect_isolated.py
    chmod -R a+rw /out
  '
