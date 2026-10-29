from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

Filename = Literal["parser.py", "stats.py", "tokenize_text.py", "metrics.py", "report.py"]


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: Filename
    instruction: str = Field(min_length=10, max_length=3000)
    depends_on: list[Filename]


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[Task] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def validate_tasks(self):
        names = {task.filename for task in self.tasks}
        if len(names) != len(self.tasks):
            raise ValueError("Task filenames must be unique")
        completed = set()
        for task in self.tasks:
            if len(set(task.depends_on)) != len(task.depends_on):
                raise ValueError("Duplicate dependencies")
            if task.filename in task.depends_on or not set(task.depends_on) <= names:
                raise ValueError("Unknown dependency or self dependency")
        while len(completed) < len(names):
            ready = {t.filename for t in self.tasks if set(t.depends_on) <= completed} - completed
            if not ready:
                raise ValueError("Dependency graph contains a cycle")
            completed.update(ready)
        return self


class SourceFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: Filename
    content: str = Field(min_length=20, max_length=24_000)


def merge_files(previous: dict[str, str], updates: dict[str, str]) -> dict[str, str]:
    return {**previous, **updates}


class RunState(TypedDict, total=False):
    requirement: str
    plan: dict
    artifacts: Annotated[dict[str, str], merge_files]
    repair_round: int
    targets: list[str]
    feedback: str
    verification: dict
    completed: list[str]
    batch: list[str]


REQUIREMENT = """Build a Python standard-library-only CSV order utility with exactly two modules.
parser.py must export parse_orders(csv_text: str) -> list[dict]. CSV headers are order_id,amount
(extra columns are allowed). Trim surrounding whitespace in header names and field values.
Each returned record has exactly order_id (nonempty str) and amount_cents (nonnegative int).
Use decimal-safe parsing: 10.50 is 1050 cents, 0 is 0, 1.2 is 120. Reject negative,
non-finite, nonnumeric and fractional-cent amounts with ValueError; reject missing required
headers or blank order IDs with ValueError. A header-only CSV yields []. Blank input is invalid.
stats.py must export summarize(orders: list[dict]) -> dict with exactly count and total_cents,
computed from these records using integer arithmetic; the empty list returns both values as 0.
The modules must be independent and contain no top-level execution, shell calls, network calls,
external dependencies, test changes, or accesses outside supplied data. Return source content,
not markdown fences. The independent acceptance suite is provided by the platform.
"""
