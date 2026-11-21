#!/bin/bash
# Records which class-level table entries each test reads at the pinned commit.
# Usage: tools/collect_entries.sh <sqlglot checkout at the pin> [number of tests]
# Writes .cache/entries.json, or .cache/entries_smoke.json with a test limit.
# Capped at 2 CPUs so it can run beside the mutation harness.
set -euo pipefail

PIN=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
IMAGE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${1:?path to sqlglot checkout}"
LIMIT="${2:-0}"
RESULT=/out/entries.json
[ "$LIMIT" = 0 ] || RESULT=/out/entries_smoke.json

[ "$(git -C "$SRC" rev-parse HEAD)" = "$PIN" ] || { echo "checkout is not at $PIN" >&2; exit 1; }

# Native arm64 because duckdb crashes under this VM's amd64 emulation.
tar -C "$SRC" --exclude .git -cf - . | docker run --rm -i --platform linux/arm64 --cpus 2 \
  --env SKIP_INTEGRATION=0 --env PYTHONPATH=/src:/tools \
  --env ENTRY_LIMIT="$LIMIT" --env ENTRY_RESULT="$RESULT" --env ENTRY_JOBS=2 \
  --volume "$ROOT/.cache:/out" --volume "$ROOT/tools:/tools:ro" \
  --volume "$ROOT/sample/tests.json:/tests.json:ro" \
  --volume "$ROOT/requirements.lock:/req.lock:ro" \
  "$IMAGE" sh -c '
    set -e
    mkdir /src && tar -xf - -C /src
    pip install -q --root-user-action=ignore -r /req.lock
    python /tools/collect_entries.py
    chmod -R a+rw /out
  '
