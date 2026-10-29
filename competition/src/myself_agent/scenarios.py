"""Bounded software tasks with trusted contracts and separate acceptance suites."""

from dataclasses import dataclass

from myself_agent.schema import REQUIREMENT, Plan

RULES = """
Use only the Python standard library. No top-level execution, shell/network calls,
external packages, test edits or host file access. Return Python source, not markdown.
Keep all specified public contracts. The platform owns independent acceptance tests.
"""


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    description: str
    requirement: str
    dependencies: dict[str, tuple[str, ...]]
    test_modules: dict[str, str]
    runner: str
    suite: str
    tests: int

    def validate_plan(self, plan: Plan):
        actual = {t.filename: set(t.depends_on) for t in plan.tasks}
        expected = {name: set(deps) for name, deps in self.dependencies.items()}
        if actual != expected:
            raise ValueError("Plan must respect the selected scenario's module contracts")

    def public(self):
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "requirement": self.requirement,
            "tasks": [
                {"filename": name, "depends_on": list(deps)}
                for name, deps in self.dependencies.items()
            ],
            "tests": self.tests,
        }


SCENARIOS = {
    "csv-orders": Scenario(
        id="csv-orders",
        title="订单 CSV 工具",
        description="解析与汇总并行开发，验证金额精度与异常输入。",
        requirement=REQUIREMENT,
        dependencies={"parser.py": (), "stats.py": ()},
        test_modules={"test_parse": "parser.py", "test_summary": "stats.py"},
        runner="runner.py",
        suite="csv-orders-v1",
        tests=12,
    ),
    "text-insights": Scenario(
        id="text-insights",
        title="文本统计工具",
        description="先完成分词模块，再开发依赖它的文本统计模块。",
        requirement="""Build exactly two Python modules for ASCII word statistics.
tokenize_text.py exports tokenize(text: str) -> list[str]: find ASCII letter runs
matching [A-Za-z]+, convert each to lowercase, preserve order; other characters are
separators. Empty or non-ASCII-only input returns [].
metrics.py imports tokenize from tokenize_text and exports analyze(text: str) -> dict
with EXACTLY words (number of tokens), unique (number of distinct tokens), frequencies
(dict token -> count). Empty input returns zero counts and an empty dict.
The metrics.py task MUST depend_on ["tokenize_text.py"]. tokenize_text.py has no dependency.
"""
        + RULES,
        dependencies={"tokenize_text.py": (), "metrics.py": ("tokenize_text.py",)},
        test_modules={"test_tokenize": "tokenize_text.py", "test_metrics": "metrics.py"},
        runner="text_runner.py",
        suite="text-insights-v1",
        tests=8,
    ),
    "order-report": Scenario(
        id="order-report",
        title="订单报告生成器",
        description="两个模块并行完成后，汇合生成 Markdown 报告。",
        requirement=REQUIREMENT.replace("exactly two modules", "exactly three modules")
        + """
Add report.py exporting build_report(csv_text: str) -> str. It MUST import parse_orders
from parser and summarize from stats, and call them to validate and aggregate input.
Return EXACTLY '# Order report\\n\\nOrders: N\\nTotal: C cents\\n' with N=count, C=total_cents.
For invalid CSV, propagate ValueError. For header-only CSV use N=0, C=0.
report.py depends_on ["parser.py", "stats.py"]. parser.py and stats.py remain independent.
""",
        dependencies={"parser.py": (), "stats.py": (), "report.py": ("parser.py", "stats.py")},
        test_modules={
            "test_parse": "parser.py",
            "test_summary": "stats.py",
            "test_report": "report.py",
        },
        runner="report_runner.py",
        suite="order-report-v1",
        tests=16,
    ),
}


def get_scenario(scenario_id: str) -> Scenario:
    try:
        return SCENARIOS[scenario_id]
    except KeyError as error:
        raise ValueError("Unknown scenario") from error
