"""pytest plugin: write which tests passed, skipped or failed in this session.

A test with a failing subtest counts as failed even when pytest also reports it
as passed. Works on the xdist controller, which receives every report.
"""

import json
import os

passed: set[str] = set()
failed: set[str] = set()


def pytest_runtest_logreport(report) -> None:
    if report.outcome == "failed":
        failed.add(report.nodeid)
    elif report.when == "call" or (report.when == "setup" and report.outcome == "skipped"):
        passed.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus) -> None:
    if hasattr(session.config, "workerinput"):
        return
    with open(os.environ["MUTATION_REPORT"], "w") as handle:
        json.dump({"passed": sorted(passed), "failed": sorted(failed)}, handle)
