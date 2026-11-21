#!/bin/bash
# Runs sqlglot's whole test suite at the pinned commit with per-test line
# coverage, in the image and package versions the commits were labeled with.
# Usage: tools/collect_coverage.sh <path to a sqlglot checkout at the pin>
# Writes .cache/coverage.db and .cache/junit.xml.
set -euo pipefail

PIN=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
IMAGE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${1:?path to sqlglot checkout}"

[ "$(git -C "$SRC" rev-parse HEAD)" = "$PIN" ] || { echo "checkout is not at $PIN" >&2; exit 1; }
mkdir -p "$ROOT/.cache"
rm -f "$ROOT"/.cache/coverage.db* "$ROOT/.cache/junit.xml"

# Native arm64 on purpose: duckdb crashes under this VM's amd64 emulation, which
# would stop test_executor.py from being collected. Line coverage is of Python
# source, and the image digest and package versions are the same.
tar -C "$SRC" --exclude .git -cf - . | docker run --rm -i --platform linux/arm64 \
  --env SKIP_INTEGRATION=0 --env PYTHONPATH=/src:/tools --env COVERAGE_FILE=/out/coverage.db \
  --volume "$ROOT/.cache:/out" --volume "$ROOT/tools:/tools:ro" --volume "$ROOT/requirements.lock:/req.lock:ro" \
  "$IMAGE" sh -c '
    set -e
    mkdir /src && tar -xf - -C /src
    pip install -q --root-user-action=ignore -r /req.lock coverage==7.13.0 pytest-cov==7.0.0
    cd /src
    status=0
    python -X faulthandler -m pytest tests -q -p inprocess_pool -p no:cacheprovider --timeout=1800 \
      --cov=sqlglot --cov=tests --cov-context=test --cov-report= \
      --junitxml=/out/junit.xml > /out/pytest.log 2>&1 || status=$?
    tail -25 /out/pytest.log
    echo "pytest exit $status"
    chmod -R a+rw /out
  '
ls -l "$ROOT/.cache"
