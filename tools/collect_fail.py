"""pytest plugin: write the node ids of tests that failed (any phase, any subtest) to $BR_FAIL_OUT."""
import os
FAILED = set()
def pytest_runtest_logreport(report):
    if report.failed:
        FAILED.add(report.nodeid.split("[")[0])
def pytest_collectreport(report):
    if report.failed:
        FAILED.add("COLLECT:" + report.nodeid)
def pytest_sessionfinish(session):
    with open(os.environ["BR_FAIL_OUT"], "w") as f:
        f.write("\n".join(sorted(FAILED)))
