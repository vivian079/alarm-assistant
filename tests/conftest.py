"""Run the code without Postgres, the Anthropic API or Docker.

api/ and eval/ modules read some settings when they are imported. These point them at the repo's own data
and at an API key that is never used: no test calls Claude, and no test connects to a database.
"""

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")  # db.py reads it on import
os.environ.setdefault("SOP_FILE", str(DATA / "sops.json"))           # retrieval.py loads the SOPs on import
os.environ.setdefault("DATA_DIR", str(DATA))
os.environ.setdefault("ANTHROPIC_API_KEY", "not-used")
os.environ.setdefault("JUDGE_PROMPT_VERSION", "judge_v3")
os.environ.setdefault("GOLDEN_SET_VERSION", "golden_v2")


def load(name: str):
    return json.loads((DATA / name).read_text())


def with_time(row: dict) -> dict:
    """JSON has the time as text; Postgres gives code a datetime."""
    return {**row, "timestamp": datetime.fromisoformat(row["timestamp"])}


@pytest.fixture(scope="session")
def alarms() -> dict:
    return {r["alarm_id"]: with_time(r) for r in load("alarm_records.json")}


@pytest.fixture(scope="session")
def codes() -> dict:
    return {c["alarm_code"]: c for c in load("alarm_codes.json")}


@pytest.fixture
def fake_db(monkeypatch, alarms):
    """db.find_earlier_duplicate and db.get_tool_events over the JSON files, with the same filters as the SQL.

    Like db.init_db, every alarm record is also an alarm event, and data/tool_events.json adds the older
    alarms and the repairs.
    """
    import db

    events = [{"event_id": a["alarm_id"], "tool_id": a["tool_id"], "chamber": a["chamber"],
               "timestamp": a["timestamp"], "event_type": "alarm", "alarm_code": a["alarm_code"], "note": None}
              for a in alarms.values()]
    events += [with_time(e) for e in load("tool_events.json")]

    def get_tool_events(tool_id, start, end):
        rows = [e for e in events if e["tool_id"] == tool_id and start <= e["timestamp"] < end]
        return sorted(rows, key=lambda e: e["timestamp"])

    def find_earlier_duplicate(alarm):
        keys = ("tool_id", "chamber", "alarm_code", "timestamp", "sensor_value")
        earlier = sorted(a["alarm_id"] for a in alarms.values()
                         if all(a[k] == alarm[k] for k in keys) and a["alarm_id"] < alarm["alarm_id"])
        return {"alarm_id": earlier[0]} if earlier else None

    monkeypatch.setattr(db, "get_tool_events", get_tool_events)
    monkeypatch.setattr(db, "find_earlier_duplicate", find_earlier_duplicate)
    return db
