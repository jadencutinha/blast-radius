"""pytest plugin for a process that runs one test: record what it depended on.

Writes the data files it opened or listed, Python files it read as text, and
the modules it loaded. Python files opened by the import system, pytest,
coverage or traceback formatting are not reads by the test.
"""

import json
import os
import sys

ROOT = "/src/"
MACHINERY = ("importlib", "/_pytest/", "/coverage/", "linecache", "tokenize")

seen: set[str] = set()
recording = True


def read_by_machinery() -> bool:
    frame = sys._getframe(2)
    for _ in range(3):
        if frame is None:
            return False
        if any(name in frame.f_code.co_filename for name in MACHINERY):
            return True
        frame = frame.f_back
    return False


def audit(event, args) -> None:
    if not recording or event not in ("open", "os.listdir", "os.scandir") or not args:
        return
    path = args[0]
    if not isinstance(path, (str, bytes, os.PathLike)):
        return
    try:
        path = os.path.abspath(os.fsdecode(path))
    except (TypeError, ValueError):
        return
    if not path.startswith(ROOT) or path.endswith(".pyc") or "__pycache__" in path:
        return
    if path.endswith(".py") and read_by_machinery():
        return
    seen.add(path[len(ROOT):])


sys.addaudithook(audit)


def pytest_sessionfinish(session, exitstatus) -> None:
    global recording
    recording = False
    modules = set()
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if path and path.startswith(ROOT) and path.endswith(".py"):
            modules.add(path[len(ROOT):])
    with open(os.environ["ISOLATED_OUTPUT"], "w") as handle:
        json.dump({"opens": sorted(seen), "modules": sorted(modules)}, handle)
