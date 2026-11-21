#!/bin/bash
# Runs each sqlglot test file in its own process at the pinned commit and records
# the modules it loaded. Usage: tools/collect_modules.sh <sqlglot checkout at the pin>
# Writes .cache/modules/<test file with / as __>.json.
set -euo pipefail

PIN=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
IMAGE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${1:?path to sqlglot checkout}"

[ "$(git -C "$SRC" rev-parse HEAD)" = "$PIN" ] || { echo "checkout is not at $PIN" >&2; exit 1; }
rm -rf "$ROOT/.cache/modules"
mkdir -p "$ROOT/.cache/modules"
python3 -c 'import json,sys; print("\n".join(sorted({t["file"] for t in json.load(open(sys.argv[1]))["tests"]})))' \
  "$ROOT/sample/tests.json" > "$ROOT/.cache/test_files.txt"

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
    cat /out/test_files.txt | xargs -P 4 -I{} sh -c "
      name=\$(echo {} | sed s#/#__#g)
      MODULES_OUTPUT=/out/modules/\$name.json python -m pytest {} -q -p inprocess_pool -p trace_modules \
        -p no:cacheprovider --timeout=1800 > /dev/null 2>&1 || echo failed {}
    "
    chmod -R a+rw /out
  '
echo "test files: $(wc -l < "$ROOT/.cache/test_files.txt"), recorded: $(ls "$ROOT/.cache/modules" | wc -l)"
