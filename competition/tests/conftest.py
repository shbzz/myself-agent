import uuid

import pytest
from myself_agent.config import Settings
from pydantic import SecretStr

PARSER = """import csv
import io
from decimal import Decimal, InvalidOperation

def parse_orders(csv_text):
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        raise ValueError("missing headers")
    reader.fieldnames = [name.strip() for name in reader.fieldnames]
    if not {"order_id", "amount"}.issubset(reader.fieldnames):
        raise ValueError("missing headers")
    result = []
    for row in reader:
        identifier = (row.get("order_id") or "").strip()
        try:
            value = Decimal((row.get("amount") or "").strip())
        except InvalidOperation as exc:
            raise ValueError("bad amount") from exc
        if not identifier or not value.is_finite() or value < 0:
            raise ValueError("invalid order")
        cents = value * 100
        if cents != cents.to_integral_value():
            raise ValueError("fractional cent")
        result.append({"order_id": identifier, "amount_cents": int(cents)})
    return result
"""

STATS = """def summarize(orders):
    return {"count": len(orders), "total_cents": sum(item["amount_cents"] for item in orders)}
"""


@pytest.fixture
def settings(tmp_path):
    return Settings(api_key=SecretStr("test-placeholder"), run_root=tmp_path)


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "workspace"
    path.mkdir(mode=0o755)
    (path / "parser.py").write_text(PARSER)
    (path / "stats.py").write_text(STATS)
    return path


@pytest.fixture
def container_name():
    return "myself-agent-" + uuid.uuid4().hex + "-0"
