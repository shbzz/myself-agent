"""Descriptive small-sample results; missing usage is never silently zero."""

from statistics import median

VARIANTS = ("single", "fixed_team", "custom")


def summarize(observations):
    seen, groups = set(), {name: [] for name in VARIANTS}
    for row in observations:
        key = (row["case_id"], row["variant"], row["repeat"])
        if key in seen:
            raise ValueError("Duplicate benchmark observation")
        seen.add(key)
        if row["variant"] not in groups or type(row["accepted"]) is not bool:
            raise ValueError("Invalid benchmark observation")
        if row["accepted"] and (
            row.get("artifact_checks_passed") is not True
            or row.get("reported_passed") is not True
            or row.get("error_type") is not None
        ):
            raise ValueError("Accepted observation contradicts its evidence")
        groups[row["variant"]].append(row)
    output = {}
    for name, rows in groups.items():
        times = [row["seconds"] for row in rows if row.get("seconds") is not None]
        calls = [row["model_calls"] for row in rows if row.get("model_calls") is not None]
        tokens = [row["total_tokens"] for row in rows if row.get("total_tokens") is not None]
        output[name] = {
            "runs": len(rows),
            "accepted": sum(row["accepted"] for row in rows),
            "not_accepted": sum(not row["accepted"] for row in rows),
            "wrong_acceptances": sum(
                row.get("reported_passed") is True and row.get("artifact_checks_passed") is False
                for row in rows
            ),
            "median_seconds": median(times) if times else None,
            "observed_model_calls": sum(calls) if calls else None,
            "missing_call_records": len(rows) - len(calls),
            "observed_total_tokens": sum(tokens) if tokens else None,
            "missing_token_records": len(rows) - len(tokens),
        }
    return {
        "variants": output,
        "comparative_superiority_established": False,
        "note": "Small exploratory observations, not a statistical superiority claim.",
    }
