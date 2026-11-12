import json
import os

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ["DATABASE_URL"]
ALARM_DATA_FILE = os.environ.get("ALARM_DATA_FILE", "/data/alarm_records.json")


def get_conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    """Create the two tables and load the synthetic alarm records (runs on API startup)."""
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alarm_records (
                alarm_id        TEXT PRIMARY KEY,
                timestamp       TIMESTAMP NOT NULL,
                tool_id         TEXT NOT NULL,
                chamber         TEXT NOT NULL,
                alarm_code      TEXT NOT NULL,
                process_step    TEXT NOT NULL,
                sensor_value    DOUBLE PRECISION NOT NULL,
                wafer_count     INTEGER NOT NULL,
                recent_history  TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS handling_plans (
                plan_id     SERIAL PRIMARY KEY,
                alarm_id    TEXT NOT NULL REFERENCES alarm_records(alarm_id),
                plan_text   TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending_review',
                created_at  TIMESTAMP NOT NULL DEFAULT now()
            )
        """)
        with open(ALARM_DATA_FILE) as f:
            records = json.load(f)
        for r in records:
            conn.execute(
                """
                INSERT INTO alarm_records
                    (alarm_id, timestamp, tool_id, chamber, alarm_code,
                     process_step, sensor_value, wafer_count, recent_history)
                VALUES
                    (%(alarm_id)s, %(timestamp)s, %(tool_id)s, %(chamber)s, %(alarm_code)s,
                     %(process_step)s, %(sensor_value)s, %(wafer_count)s, %(recent_history)s)
                ON CONFLICT (alarm_id) DO NOTHING
                """,
                r,
            )


def list_alarms() -> list[dict]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM alarm_records ORDER BY alarm_id").fetchall()


def get_alarm(alarm_id: str) -> dict | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM alarm_records WHERE alarm_id = %s", (alarm_id,)
        ).fetchone()


def save_plan(alarm_id: str, plan_text: str) -> dict:
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO handling_plans (alarm_id, plan_text) VALUES (%s, %s) RETURNING *",
            (alarm_id, plan_text),
        ).fetchone()


def approve_plan(plan_id: int) -> dict | None:
    with get_conn() as conn:
        return conn.execute(
            "UPDATE handling_plans SET status = 'approved' WHERE plan_id = %s RETURNING *",
            (plan_id,),
        ).fetchone()
