"""Dependency-ready batches, prioritizing longer remaining dependency paths."""

from myself_agent.schema import Plan


def ready_batch(plan: Plan, targets: list[str], completed: list[str], concurrency: int):
    tasks = {task.filename: task for task in plan.tasks}
    pending = set(targets) - set(completed)
    if not pending:
        return []
    satisfied = (set(tasks) - set(targets)) | set(completed)

    def depth(name):
        children = [
            t.filename for t in plan.tasks if name in t.depends_on and t.filename in pending
        ]
        return 1 + max((depth(child) for child in children), default=0)

    ready = [name for name in pending if set(tasks[name].depends_on) <= satisfied]
    if not ready:
        raise ValueError("No dependency-ready task; refusing a deadlocked schedule")
    return sorted(ready, key=lambda name: (-depth(name), name))[:concurrency]


def affected_tasks(plan: Plan, roots: set[str]) -> list[str]:
    """Retest all modules; regenerate roots plus every transitive dependant."""
    affected = set(roots)
    while True:
        expanded = affected | {t.filename for t in plan.tasks if set(t.depends_on) & affected}
        if expanded == affected:
            return sorted(affected)
        affected = expanded
