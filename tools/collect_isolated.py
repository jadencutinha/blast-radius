"""Run every test alone in a fresh process, inside the container.

A shared process runs class setup, caches and lazy imports once, for whichever
test came first. Alone, each test pays for everything it depends on. Writes
/out/isolated.json: {"lines": {path: {mask hex: [lines]}}, "opens": {path: hex},
"modules": {path: hex}, "failed": [test ids]}.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

WORK = Path("/tmp/isolated")
RC = WORK / "coveragerc"
JOBS = 4


def numbits(blob: bytes):
    for index, byte in enumerate(blob):
        for bit in range(8):
            if byte & (1 << bit):
                yield index * 8 + bit


def main() -> None:
    tests = json.load(open("/tests.json"))["tests"]
    limit = int(os.environ.get("ISOLATED_LIMIT", "0"))
    if limit:
        step = max(len(tests) // limit, 1)
        tests = tests[::step][:limit]
    WORK.mkdir(parents=True, exist_ok=True)
    RC.write_text(
        "[run]\nsource = /src/sqlglot,/src/tests\nparallel = true\ncore = sysmon\n"
        "patch = subprocess\ndisable_warnings = no-data-collected,module-not-measured\n"
    )

    blobs: dict[tuple[str, bytes], int] = defaultdict(int)
    opens: dict[str, int] = defaultdict(int)
    modules: dict[str, int] = defaultdict(int)
    failed: list[str] = []
    lock = threading.Lock()
    done = [0]

    def run(test: dict) -> None:
        number = test["number"]
        folder = WORK / str(number)
        folder.mkdir()
        output = folder / "deps.json"
        env = dict(os.environ, COVERAGE_FILE=str(folder / "cov"), ISOLATED_OUTPUT=str(output))
        command = [sys.executable, "-m", "coverage", "run", f"--rcfile={RC}", "-m", "pytest",
                   test["id"], "-q", "-p", "inprocess_pool", "-p", "trace_isolated",
                   "-p", "no:cacheprovider", "--timeout=900"]
        result = subprocess.run(command, cwd="/src", env=env, capture_output=True, text=True)
        rows = []
        for path in folder.glob("cov*"):
            db = sqlite3.connect(path)
            rows += db.execute(
                "select f.path, l.numbits from line_bits l join file f on f.id = l.file_id"
            ).fetchall()
            db.close()
        deps = json.loads(output.read_text()) if output.exists() else None
        bit = 1 << number
        with lock:
            for path, blob in rows:
                blobs[(path.removeprefix("/src/"), bytes(blob))] |= bit
            if deps:
                for path in deps["opens"]:
                    opens[path] |= bit
                for path in deps["modules"]:
                    modules[path] |= bit
            if result.returncode != 0 or not deps or not rows:
                failed.append(test["id"])
            done[0] += 1
            if done[0] % 50 == 0:
                print(f"{done[0]}/{len(tests)} done, {len(failed)} failed", flush=True)
        shutil.rmtree(folder, ignore_errors=True)

    with ThreadPoolExecutor(JOBS) as pool:
        list(pool.map(run, tests))

    lines: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for (path, blob), mask in blobs.items():
        for line in numbits(blob):
            lines[path][line] |= mask
    grouped: dict[str, dict[str, list[int]]] = {}
    for path, by_line in lines.items():
        groups: dict[int, list[int]] = defaultdict(list)
        for line, mask in by_line.items():
            groups[mask].append(line)
        grouped[path] = {format(mask, "x"): sorted(group) for mask, group in groups.items()}
    result = {
        "lines": grouped,
        "opens": {path: format(mask, "x") for path, mask in opens.items()},
        "modules": {path: format(mask, "x") for path, mask in modules.items()},
        "failed": sorted(failed),
        "tests": len(tests),
    }
    with open(os.environ.get("ISOLATED_RESULT", "/out/isolated.json"), "w") as handle:
        json.dump(result, handle, separators=(",", ":"))
    print(f"finished {len(tests)} tests, {len(failed)} failed: {sorted(failed)[:10]}", flush=True)


if __name__ == "__main__":
    main()
