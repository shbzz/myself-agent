import io
import json
import sys
import unittest

sys.path.insert(0, "/acceptance")
from runner import Acceptance


class ReportAcceptance(Acceptance):
    def report(self, csv_text):
        from report import build_report

        return build_report(csv_text)

    def test_report_basic(self):
        self.assertEqual(
            self.report("order_id,amount\nA,1.20\nB,2.30\n"),
            "# Order report\n\nOrders: 2\nTotal: 350 cents\n",
        )

    def test_report_empty(self):
        self.assertEqual(
            self.report("order_id,amount\n"), "# Order report\n\nOrders: 0\nTotal: 0 cents\n"
        )

    def test_report_invalid(self):
        with self.assertRaises(ValueError):
            self.report("order_id,amount\nA,-1\n")

    def test_report_dependencies(self):
        from unittest.mock import patch

        import report

        with (
            patch.object(report, "parse_orders", return_value=[{"amount_cents": 7}]) as parse,
            patch.object(
                report, "summarize", return_value={"count": 9, "total_cents": 42}
            ) as stats,
        ):
            self.assertEqual(
                report.build_report("placeholder"), "# Order report\n\nOrders: 9\nTotal: 42 cents\n"
            )
            parse.assert_called_once_with("placeholder")
            stats.assert_called_once_with([{"amount_cents": 7}])


if __name__ == "__main__":
    result = unittest.TextTestRunner(stream=io.StringIO()).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ReportAcceptance)
    )
    report = {
        "suite": "order-report-v1",
        "tests_run": result.testsRun,
        "passed": result.wasSuccessful() and result.testsRun == 16,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "failed_cases": [
            {"test": test.id(), "traceback": detail[-6000:]}
            for test, detail in result.failures + result.errors
        ],
    }
    print("MYSELF_AGENT_TEST_RESULT:" + json.dumps(report), flush=True)
    raise SystemExit(0 if report["passed"] else 1)
