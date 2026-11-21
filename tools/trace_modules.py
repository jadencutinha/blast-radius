"""pytest plugin: write the sqlglot and test modules loaded by this session.

Run on one test file per process. sqlglot imports its dialects lazily, so a
static import graph cannot see which test files depend on them.
"""

import json
import os
import sys


def pytest_sessionfinish(session, exitstatus) -> None:
    loaded = set()
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if path and path.startswith("/src/") and path.endswith(".py"):
            loaded.add(path[len("/src/"):])
    with open(os.environ["MODULES_OUTPUT"], "w") as handle:
        json.dump(sorted(loaded), handle)
