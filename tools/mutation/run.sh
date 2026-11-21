#!/bin/bash
# Runs the real sqlglot suite on each patch listed in .cache/mutants/order.txt.
# Usage: tools/mutation/run.sh <sqlglot checkout at the pin> [max patches this run]
# Results append to .cache/mutation_results.jsonl and a rerun resumes.
set -euo pipefail

PIN=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
IMAGE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="${1:?path to sqlglot checkout}"
LIMIT="${2:-0}"

[ "$(git -C "$SRC" rev-parse HEAD)" = "$PIN" ] || { echo "checkout is not at $PIN" >&2; exit 1; }

# Native arm64 because duckdb crashes under this VM's amd64 emulation.
tar -C "$SRC" --exclude .git -cf - . | docker run --rm -i --platform linux/arm64 \
  --env MUTATION_LIMIT="$LIMIT" \
  --volume "$ROOT/.cache:/out" --volume "$ROOT/.cache/mutants:/mutants:ro" \
  --volume "$ROOT/tools/mutation:/tools/mutation:ro" \
  --volume "$ROOT/sample/tests.json:/tests.json:ro" \
  --volume "$ROOT/requirements.lock:/req.lock:ro" \
  "$IMAGE" sh -c '
    set -e
    mkdir /src && tar -xf - -C /src
    cp -a /src /pristine
    pip install -q --root-user-action=ignore -r /req.lock
    python /tools/mutation/run_suite.py
    chmod a+rw /out/mutation_results.jsonl
  '
