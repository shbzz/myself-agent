import io
import json
import sys
import unittest

sys.path.insert(0, "/workspace")


class TextAcceptance(unittest.TestCase):
    def tokens(self, text):
        from tokenize_text import tokenize

        return tokenize(text)

    def analyze(self, text):
        from metrics import analyze

        return analyze(text)

    def test_tokenize_basic(self):
        self.assertEqual(self.tokens("Hello, WORLD! hello"), ["hello", "world", "hello"])

    def test_tokenize_separators(self):
        self.assertEqual(self.tokens("one2two_three\nfour's"), ["one", "two", "three", "four", "s"])

    def test_tokenize_empty(self):
        for value in ["", "  ", "123", "中文"]:
            with self.subTest(value=value):
                self.assertEqual(self.tokens(value), [])

    def test_tokenize_mixed(self):
        self.assertEqual(self.tokens("中文ABC中文def"), ["abc", "def"])

    def test_metrics_basic(self):
        self.assertEqual(
            self.analyze("A a b"), {"words": 3, "unique": 2, "frequencies": {"a": 2, "b": 1}}
        )

    def test_metrics_empty(self):
        self.assertEqual(self.analyze(""), {"words": 0, "unique": 0, "frequencies": {}})

    def test_metrics_import_contract(self):
        from unittest.mock import patch

        import metrics

        with patch.object(metrics, "tokenize", return_value=["stub", "stub"]):
            self.assertEqual(
                metrics.analyze("ignored"), {"words": 2, "unique": 1, "frequencies": {"stub": 2}}
            )

    def test_metrics_many(self):
        self.assertEqual(
            self.analyze("x " * 1000), {"words": 1000, "unique": 1, "frequencies": {"x": 1000}}
        )


if __name__ == "__main__":
    result = unittest.TextTestRunner(stream=io.StringIO()).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(TextAcceptance)
    )
    report = {
        "suite": "text-insights-v1",
        "tests_run": result.testsRun,
        "passed": result.wasSuccessful() and result.testsRun == 8,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "failed_cases": [
            {"test": test.id(), "traceback": detail[-6000:]}
            for test, detail in result.failures + result.errors
        ],
    }
    print("MYSELF_AGENT_TEST_RESULT:" + json.dumps(report), flush=True)
    raise SystemExit(0 if report["passed"] else 1)
