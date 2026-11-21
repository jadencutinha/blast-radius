"""Inside the container: run each test alone and record the table entries it reads.

Writes /out/entries.json (or ENTRY_RESULT): {"entries": {id: test mask hex},
"failed": [test ids], "tests": count}.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, "/tools")
from table_scan import scan  # noqa: E402

WORK = Path("/tmp/entries")
JOBS = int(os.environ.get("ENTRY_JOBS", "2"))


def main() -> None:
    tests = json.load(open("/tests.json"))["tests"]
    limit = int(os.environ.get("ENTRY_LIMIT", "0"))
    if limit:
        step = max(len(tests) // limit, 1)
        tests = tests[::step][:limit]
    WORK.mkdir(parents=True, exist_ok=True)
    scan_path = WORK / "scan.json"
    scan_path.write_text(json.dumps(scan(Path("/src"))))

    masks: dict[str, int] = defaultdict(int)
    failed: list[str] = []
    lock = threading.Lock()
    done = [0]

    def run(test: dict) -> None:
        output = WORK / f"{test['number']}.json"
        env = dict(os.environ, TABLE_SCAN=str(scan_path), ENTRY_OUTPUT=str(output),
                   PYTHONDONTWRITEBYTECODE="1")
        command = [sys.executable, "-m", "pytest", test["id"], "-q", "-p", "inprocess_pool",
                   "-p", "trace_entries", "-p", "no:cacheprovider", "--timeout=900"]
        result = subprocess.run(command, cwd="/src", env=env, capture_output=True, text=True)
        read = json.loads(output.read_text()) if output.exists() else None
        with lock:
            if read is not None:
                for entry in read:
                    masks[entry] |= 1 << test["number"]
            if result.returncode != 0 or read is None:
                failed.append(test["id"])
                (Path("/out") / "entries_failure.log").write_text(result.stdout[-3000:] + result.stderr[-1500:])
            done[0] += 1
            if done[0] % 50 == 0:
                print(f"{done[0]}/{len(tests)} done, {len(failed)} failed", flush=True)
        output.unlink(missing_ok=True)

    with ThreadPoolExecutor(JOBS) as pool:
        list(pool.map(run, tests))
    shutil.rmtree(WORK, ignore_errors=True)
    result_path = os.environ.get("ENTRY_RESULT", "/out/entries.json")
    with open(result_path, "w") as handle:
        json.dump({"entries": {key: format(mask, "x") for key, mask in masks.items()},
                   "failed": sorted(failed), "tests": len(tests)}, handle, separators=(",", ":"))
    print(f"finished {len(tests)} tests, {len(failed)} failed: {sorted(failed)[:8]}", flush=True)


if __name__ == "__main__":
    main()
