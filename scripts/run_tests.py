"""Run offline tests; an empty selection must fail rather than appear green."""

import argparse
import os
from pathlib import Path
import sys
import unittest

from workflow import save_json

ROOT = Path(__file__).resolve().parents[1]


class RecordingResult(unittest.TextTestResult):
    """Capture parent test outcomes, including skipped/failed subtests."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = {}

    def record(self, test, status, reason=None):
        test_id = getattr(test, "test_case", test).id()
        ranks = {
            "passed": 0,
            "skipped": 1,
            "expected_failure": 2,
            "unexpected_success": 3,
            "failed": 4,
            "error": 5,
        }
        old = self.cases.get(test_id)
        if old and ranks[old["status"]] > ranks[status]:
            return
        self.cases[test_id] = {"id": test_id, "status": status}
        if reason:
            self.cases[test_id]["reason"] = str(reason)[:500]

    def addSuccess(self, test):
        super().addSuccess(test)
        self.record(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.record(test, "failed")

    def addError(self, test, err):
        super().addError(test, err)
        self.record(test, "error")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.record(test, "skipped", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.record(test, "expected_failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.record(test, "unexpected_success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.record(
                test, "failed" if issubclass(err[0], test.failureException) else "error"
            )


def main(names=None, report=None):
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / "tests"))
    sys.path.insert(0, str(ROOT / "src"))
    loader = unittest.TestLoader()
    suite = (
        loader.loadTestsFromNames(names)
        if names
        else loader.discover(str(ROOT / "tests"))
    )
    if suite.countTestCases() == 0:
        if report:
            save_json(Path(report), {"tests_run": 0, "cases": []})
        print("No tests selected; refusing a false success.", file=sys.stderr)
        return 1
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(
        suite
    )
    if report:
        save_json(
            Path(report),
            {
                "tests_run": result.testsRun,
                "skipped": len(result.skipped),
                "cases": list(result.cases.values()),
            },
        )
    return int(not result.wasSuccessful())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    raise SystemExit(main(args.names, args.report))
