"""pytest plugin: append each test's outcome to a file as it is reported.

A test with a failing subtest counts as failed even when pytest also reports it
as passed. Only the xdist controller writes, and it receives every report, so a
run killed at its time limit still leaves everything reported until then.
"""

import os

handle = None


def pytest_configure(config) -> None:
    global handle
    if not hasattr(config, "workerinput"):
        handle = open(os.environ["MUTATION_REPORT"], "a", buffering=1)


def pytest_runtest_logreport(report) -> None:
    if handle is None:
        return
    if report.outcome == "failed":
        handle.write(f"F\t{report.nodeid}\n")
    elif report.when == "call" or (report.when == "setup" and report.outcome == "skipped"):
        handle.write(f"P\t{report.nodeid}\n")
