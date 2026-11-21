"""Inside the container: apply each patch, run the real suite, record failures.

A test counts as failing if pytest reports a failure, error or timeout for it,
or if it never reports at all, which covers files that cannot be imported and
crashed workers. Results append to /out/mutation_results.jsonl, so a stopped run
resumes where it left off.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

SRC = Path("/src")
PRISTINE = Path("/pristine")
RESULTS = Path("/out/mutation_results.jsonl")
REPORT = "/tmp/mutation_report.json"
HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
TEST_TIMEOUT = 240
RUN_TIMEOUT = 480


def apply_patch(text: str) -> list[str]:
    """Apply a git patch to /src and return the paths to restore afterwards."""
    touched: list[str] = []
    sections = re.split(r"(?m)^(?=diff --git )", text)
    for section in sections:
        if not section.startswith("diff --git "):
            continue
        rows = section.split("\n")
        match = re.fullmatch(r"diff --git a/(.+) b/(.+)", rows[0])
        old, new = match[1], match[2]
        touched += [old, new]
        if "\ndeleted file mode" in section:
            (SRC / old).unlink()
            continue
        if old != new:
            (SRC / new).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(SRC / old, SRC / new)
        target = SRC / new
        creating = "\nnew file mode" in section
        lines = [] if creating else target.read_text().split("\n")
        output: list[str] = []
        position = 0
        index = 0
        while index < len(rows):
            hunk = HUNK.match(rows[index])
            index += 1
            if not hunk:
                continue
            start = int(hunk[1])
            old_left = int(hunk[2]) if hunk[2] is not None else 1
            new_left = int(hunk[4]) if hunk[4] is not None else 1
            if old_left == 0:
                start += 1
            output.extend(lines[position:start - 1])
            position = start - 1
            while old_left > 0 or new_left > 0:
                row = rows[index]
                index += 1
                if row.startswith("\\"):
                    continue
                if row.startswith("+"):
                    output.append(row[1:])
                    new_left -= 1
                    continue
                expected = row[1:]
                if position >= len(lines) or lines[position] != expected:
                    raise ValueError(f"context mismatch in {new} at line {position + 1}")
                if row.startswith(" ") or row == "":
                    output.append(lines[position])
                    new_left -= 1
                position += 1
                old_left -= 1
        output.extend(lines[position:])
        if creating:
            target.parent.mkdir(parents=True, exist_ok=True)
        if "@@ " in section:
            target.write_text("\n".join(output))
    return touched


def restore(paths: list[str]) -> None:
    for path in set(paths):
        current, original = SRC / path, PRISTINE / path
        if original.exists():
            current.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, current)
        elif current.exists():
            current.unlink()


def run_suite(tests: list[dict]) -> tuple[list[int], str]:
    Path(REPORT).unlink(missing_ok=True)
    env = dict(os.environ, MUTATION_REPORT=REPORT, SKIP_INTEGRATION="0", PYTHONPATH="/src:/tools/mutation",
               PYTHONDONTWRITEBYTECODE="1")
    command = [sys.executable, "-m", "pytest", "tests", "-n", "4", "-q", "-p", "report_plugin",
               "-p", "no:cacheprovider", f"--timeout={TEST_TIMEOUT}"]
    process = subprocess.Popen(command, cwd=SRC, env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    status = "ok"
    try:
        process.wait(RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        status = "run_timeout"
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
    if not Path(REPORT).exists():
        return [test["number"] for test in tests], "no_report" if status == "ok" else status
    report = json.loads(Path(REPORT).read_text())
    passed, failed = set(report["passed"]), set(report["failed"])
    return [test["number"] for test in tests if test["id"] in failed or test["id"] not in passed], status


def main() -> None:
    tests = json.load(open("/tests.json"))["tests"]
    order = [line.strip() for line in open("/mutants/order.txt") if line.strip()]
    done = set()
    if RESULTS.exists():
        done = {json.loads(line)["id"] for line in RESULTS.read_text().splitlines()}
    limit = int(os.environ.get("MUTATION_LIMIT", "0"))
    ran = 0
    for name in order:
        if name in done:
            continue
        if limit and ran >= limit:
            break
        started = time.time()
        touched: list[str] = []
        try:
            if name != "baseline":
                touched = apply_patch(Path(f"/mutants/{name}.patch").read_text())
            failing, status = run_suite(tests)
        except (ValueError, OSError, IndexError, TypeError) as error:
            failing, status = [], f"apply_failed: {error}"
        finally:
            for path in touched:
                pass
            restore(touched)
        with RESULTS.open("a") as handle:
            handle.write(json.dumps({"id": name, "failing": failing, "status": status,
                                     "seconds": round(time.time() - started, 1)}) + "\n")
        ran += 1
        print(f"{name} {status} failing={len(failing)} {time.time() - started:.0f}s", flush=True)


if __name__ == "__main__":
    main()
