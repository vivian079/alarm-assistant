import json
import os

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

DATABASE_URL = os.environ["DATABASE_URL"]
ALARM_DATA_FILE = os.environ.get("ALARM_DATA_FILE", "/data/alarm_records.json")
ALARM_CODE_FILE = os.environ.get("ALARM_CODE_FILE", "/data/alarm_codes.json")


def get_conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    """Create the tables and load the synthetic data (runs on API startup)."""
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
            CREATE TABLE IF NOT EXISTS alarm_codes (
                alarm_code      TEXT PRIMARY KEY,
                equipment_type  TEXT NOT NULL,
                description     TEXT NOT NULL,
                parameter       TEXT NOT NULL,
                spec_lower      DOUBLE PRECISION NOT NULL,
                spec_upper      DOUBLE PRECISION NOT NULL,
                unit            TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS handling_plans (
                plan_id     SERIAL PRIMARY KEY,
                alarm_id    TEXT NOT NULL REFERENCES alarm_records(alarm_id),
                plan_text   TEXT,
                plan_json   JSONB,
                error       TEXT,
                attempts    INTEGER,
                status      TEXT NOT NULL DEFAULT 'pending_review',
                created_at  TIMESTAMP NOT NULL DEFAULT now()
            )
        """)
        # The table may already exist from the free-text version (plan_text NOT NULL, no plan_json).
        # These bring an old table to the new shape and do nothing on a new one.
        # plan_text stays so the old free-text plans are kept.
        conn.execute("ALTER TABLE handling_plans ALTER COLUMN plan_text DROP NOT NULL")
        conn.execute("ALTER TABLE handling_plans ADD COLUMN IF NOT EXISTS plan_json JSONB")
        conn.execute("ALTER TABLE handling_plans ADD COLUMN IF NOT EXISTS error TEXT")
        conn.execute("ALTER TABLE handling_plans ADD COLUMN IF NOT EXISTS attempts INTEGER")
        # Which prompt and model made this plan, and the key to its rows in llm_calls.
        conn.execute("ALTER TABLE handling_plans ADD COLUMN IF NOT EXISTS request_id TEXT")
        conn.execute("ALTER TABLE handling_plans ADD COLUMN IF NOT EXISTS prompt_version TEXT")
        conn.execute("ALTER TABLE handling_plans ADD COLUMN IF NOT EXISTS model TEXT")

        # Audit log: one row per Claude API call, failed calls included.
        # Only INSERT into this table, never UPDATE or DELETE: it has to show what was really sent and received.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_calls (
                call_id         SERIAL PRIMARY KEY,
                called_at       TIMESTAMP NOT NULL DEFAULT now(),
                request_id      TEXT NOT NULL,   -- same value as handling_plans.request_id
                alarm_id        TEXT NOT NULL REFERENCES alarm_records(alarm_id),
                attempt         INTEGER NOT NULL, -- 1, 2, 3 within one plan generation
                prompt_version  TEXT NOT NULL,
                model           TEXT NOT NULL,
                system_prompt   TEXT NOT NULL,   -- the full text sent, SOP library included
                messages        JSONB NOT NULL,  -- the full messages sent (a retry includes the earlier output and error)
                max_tokens      INTEGER NOT NULL,
                response_text   TEXT,            -- the raw output; empty if the call itself failed
                stop_reason     TEXT,            -- end_turn / max_tokens / refusal
                input_tokens    INTEGER,
                output_tokens   INTEGER,
                latency_ms      INTEGER,
                error           TEXT             -- why this call did not give a plan; empty if it did
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

        with open(ALARM_CODE_FILE) as f:
            codes = json.load(f)
        for c in codes:
            conn.execute(
                """
                INSERT INTO alarm_codes
                    (alarm_code, equipment_type, description, parameter,
                     spec_lower, spec_upper, unit)
                VALUES
                    (%(alarm_code)s, %(equipment_type)s, %(description)s, %(parameter)s,
                     %(spec_lower)s, %(spec_upper)s, %(unit)s)
                ON CONFLICT (alarm_code) DO NOTHING
                """,
                c,
            )


def list_alarms() -> list[dict]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM alarm_records ORDER BY alarm_id").fetchall()


def get_alarm(alarm_id: str) -> dict | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM alarm_records WHERE alarm_id = %s", (alarm_id,)
        ).fetchone()


def get_alarm_code(alarm_code: str) -> dict | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM alarm_codes WHERE alarm_code = %s", (alarm_code,)
        ).fetchone()


def save_plan(alarm_id: str, plan_json: dict, attempts: int,
              request_id: str, prompt_version: str, model: str) -> dict:
    """A plan that passed validation. status starts as pending_review."""
    with get_conn() as conn:
        return conn.execute(
            """
            INSERT INTO handling_plans (alarm_id, plan_json, attempts, request_id, prompt_version, model)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING *
            """,
            (alarm_id, Jsonb(plan_json), attempts, request_id, prompt_version, model),
        ).fetchone()


def save_failed_plan(alarm_id: str, error: str, attempts: int,
                     request_id: str, prompt_version: str, model: str) -> dict:
    """Every try failed: keep a row so the engineer sees it and handles the alarm manually."""
    with get_conn() as conn:
        return conn.execute(
            """
            INSERT INTO handling_plans (alarm_id, error, attempts, status, request_id, prompt_version, model)
            VALUES (%s, %s, %s, 'generation_failed', %s, %s, %s) RETURNING *
            """,
            (alarm_id, error, attempts, request_id, prompt_version, model),
        ).fetchone()


def log_llm_call(call: dict) -> None:
    """One row in llm_calls. Keys that are missing (e.g. response_text when the call failed) are stored as NULL."""
    row = {
        "response_text": None, "stop_reason": None, "input_tokens": None,
        "output_tokens": None, "latency_ms": None, "error": None,
        **call,
        "messages": Jsonb(call["messages"]),
    }
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO llm_calls
                (request_id, alarm_id, attempt, prompt_version, model, system_prompt, messages,
                 max_tokens, response_text, stop_reason, input_tokens, output_tokens, latency_ms, error)
            VALUES
                (%(request_id)s, %(alarm_id)s, %(attempt)s, %(prompt_version)s, %(model)s, %(system_prompt)s,
                 %(messages)s, %(max_tokens)s, %(response_text)s, %(stop_reason)s, %(input_tokens)s,
                 %(output_tokens)s, %(latency_ms)s, %(error)s)
            """,
            row,
        )


def approve_plan(plan_id: int) -> dict | None:
    """Only a plan waiting for review can be approved (not a failed one)."""
    with get_conn() as conn:
        return conn.execute(
            """
            UPDATE handling_plans SET status = 'approved'
            WHERE plan_id = %s AND status = 'pending_review' RETURNING *
            """,
            (plan_id,),
        ).fetchone()
