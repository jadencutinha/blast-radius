"""pytest plugin: record which non-Python files each test opens or lists.

Files read while a test module is being imported are recorded against that
whole module as "collect:<file>", because every test in it depends on them.
"""

import json
import os
import sys

ROOT = "/src/"
OUTPUT = "/out/opens.json"

current = None
seen: dict[str, set[str]] = {}


def record(path) -> None:
    if current is None or not isinstance(path, (str, bytes, os.PathLike)):
        return
    try:
        path = os.path.abspath(os.fsdecode(path))
    except (TypeError, ValueError):
        return
    if path.startswith(ROOT) and not path.endswith((".py", ".pyc")) and "__pycache__" not in path:
        seen.setdefault(path[len(ROOT):], set()).add(current)


def audit(event, args) -> None:
    if event in ("open", "os.listdir", "os.scandir") and args:
        record(args[0])


sys.addaudithook(audit)


def pytest_collectstart(collector) -> None:
    global current
    path = getattr(collector, "path", None)
    if path is not None and str(path).endswith(".py"):
        current = "collect:" + os.path.relpath(str(path), ROOT)


def pytest_collection_finish(session) -> None:
    global current
    current = None


def pytest_runtest_logstart(nodeid, location) -> None:
    global current
    current = nodeid


def pytest_runtest_logfinish(nodeid, location) -> None:
    global current
    current = None


def pytest_sessionfinish(session, exitstatus) -> None:
    global current
    current = None
    with open(OUTPUT, "w") as handle:
        json.dump({path: sorted(ids) for path, ids in sorted(seen.items())}, handle)
