"""Trusted, read-only acceptance runner. Executed only in the restricted container."""

import io
import json
import sys
import unittest

sys.path.insert(0, "/workspace")


class Acceptance(unittest.TestCase):
    def parse(self, text):
        from parser import parse_orders

        return parse_orders(text)

    def summary(self, orders):
        from stats import summarize

        return summarize(orders)

    def test_parse_basic(self):
        self.assertEqual(
            self.parse("order_id,amount\nA,10.50\nB,0\n"),
            [{"order_id": "A", "amount_cents": 1050}, {"order_id": "B", "amount_cents": 0}],
        )

    def test_parse_whitespace_and_extra_columns(self):
        self.assertEqual(
            self.parse(" order_id , amount ,note\n A , 1.20 ,hello\n"),
            [{"order_id": "A", "amount_cents": 120}],
        )

    def test_parse_header_only(self):
        self.assertEqual(self.parse("order_id,amount\n"), [])

    def test_parse_missing_headers(self):
        for text in ["id,amount\nA,1\n", "order_id,value\nA,1\n"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.parse(text)

    def test_parse_blank_input(self):
        with self.assertRaises(ValueError):
            self.parse("")

    def test_parse_blank_id(self):
        with self.assertRaises(ValueError):
            self.parse("order_id,amount\n ,1\n")

    def test_parse_invalid_amounts(self):
        for amount in ["-1", "NaN", "Infinity", "abc", "1.001", ""]:
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                self.parse("order_id,amount\nA," + amount + "\n")

    def test_parse_decimal_precision(self):
        self.assertEqual(
            [row["amount_cents"] for row in self.parse("order_id,amount\nA,1.2\nB,0.10\n")],
            [120, 10],
        )

    def test_summary_empty(self):
        self.assertEqual(self.summary([]), {"count": 0, "total_cents": 0})

    def test_summary_integer_precision(self):
        self.assertEqual(
            self.summary([{"amount_cents": 10**18}, {"amount_cents": 1}]),
            {"count": 2, "total_cents": 10**18 + 1},
        )

    def test_summary_basic(self):
        self.assertEqual(
            self.summary([{"amount_cents": 1050}, {"amount_cents": 725}]),
            {"count": 2, "total_cents": 1775},
        )

    def test_integration(self):
        self.assertEqual(
            self.summary(self.parse("order_id,amount\nA,0.10\nB,0.20\n")),
            {"count": 2, "total_cents": 30},
        )


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Acceptance)
    result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
    cases = [
        {"test": test.id(), "traceback": detail[-6000:]}
        for test, detail in result.failures + result.errors
    ]
    report = {
        "suite": "csv-orders-v1",
        "tests_run": result.testsRun,
        "passed": result.wasSuccessful() and result.testsRun == 12,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "failed_cases": cases,
    }
    print("MYSELF_AGENT_TEST_RESULT:" + json.dumps(report), flush=True)
    raise SystemExit(0 if report["passed"] else 1)
