import json
import os

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

DATABASE_URL = os.environ["DATABASE_URL"]
ALARM_DATA_FILE = os.environ.get("ALARM_DATA_FILE", "/data/alarm_records.json")
ALARM_CODE_FILE = os.environ.get("ALARM_CODE_FILE", "/data/alarm_codes.json")
TOOL_EVENT_FILE = os.environ.get("TOOL_EVENT_FILE", "/data/tool_events.json")


def get_conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    """Create the tables and load the synthetic data (runs on API startup)."""
    with get_conn() as conn:
        # pgvector: adds the vector column type and the <=> (cosine distance) operator.
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
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
        # Retrieval. retrieved_sop_ids: the SOPs exact match put into the prompt (NULL for prompt versions
        # that send the whole library). sop_suggestions: only for status sop_not_covered, the SOPs closest
        # in meaning, shown to the engineer and never sent to Claude.
        conn.execute("ALTER TABLE handling_plans ADD COLUMN IF NOT EXISTS retrieved_sop_ids TEXT[]")
        conn.execute("ALTER TABLE handling_plans ADD COLUMN IF NOT EXISTS sop_suggestions JSONB")
        # Design doc section 7 rules, checked by code (rules.py): [{"code": "R1", "message": "...", "related": [...]}, ...]
        conn.execute("ALTER TABLE handling_plans ADD COLUMN IF NOT EXISTS flags JSONB")

        # Every alarm and repair on each tool, one row per event. Rules R1 and W1 look here.
        # Each alarm record is also an alarm event (event_id = alarm_id); tool_events.json adds the
        # older alarms and the repairs that have no alarm record.
        # recent_history (free text) stays in alarm_records for the prompt; code never parses it.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tool_events (
                event_id    TEXT PRIMARY KEY,
                tool_id     TEXT NOT NULL,
                chamber     TEXT NOT NULL,
                timestamp   TIMESTAMP NOT NULL,
                event_type  TEXT NOT NULL,   -- alarm / repair
                alarm_code  TEXT,            -- alarms only
                note        TEXT NOT NULL DEFAULT ''   -- repairs: what was done
            )
        """)

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

        # One vector per SOP (title + steps). content_hash finds SOPs whose text changed since they were embedded.
        # 40 rows: Postgres compares the query with every row. With many thousands, add an index:
        # CREATE INDEX ON sop_embeddings USING hnsw (embedding vector_cosine_ops)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sop_embeddings (
                sop_id          TEXT PRIMARY KEY,
                equipment_type  TEXT NOT NULL,
                model           TEXT NOT NULL,   -- embedding model; vectors from different models can't be compared
                content_hash    TEXT NOT NULL,
                embedding       vector(384) NOT NULL
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

        for r in records:
            conn.execute(
                """
                INSERT INTO tool_events (event_id, tool_id, chamber, timestamp, event_type, alarm_code)
                VALUES (%(alarm_id)s, %(tool_id)s, %(chamber)s, %(timestamp)s, 'alarm', %(alarm_code)s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                r,
            )
        with open(TOOL_EVENT_FILE) as f:
            events = json.load(f)
        for e in events:
            conn.execute(
                """
                INSERT INTO tool_events (event_id, tool_id, chamber, timestamp, event_type, alarm_code, note)
                VALUES (%(event_id)s, %(tool_id)s, %(chamber)s, %(timestamp)s, %(event_type)s, %(alarm_code)s, %(note)s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                e,
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


def save_plan(alarm_id: str, plan_json: dict, attempts: int, request_id: str,
              prompt_version: str, model: str, retrieved_sop_ids: list[str] | None, flags: list[dict]) -> dict:
    """A plan that passed validation. status starts as pending_review."""
    with get_conn() as conn:
        return conn.execute(
            """
            INSERT INTO handling_plans
                (alarm_id, plan_json, attempts, request_id, prompt_version, model, retrieved_sop_ids, flags)
            VALUES (%s, %s, %s, %s, %s, %s, %s::text[], %s) RETURNING *
            """,
            (alarm_id, Jsonb(plan_json), attempts, request_id, prompt_version, model, retrieved_sop_ids, Jsonb(flags)),
        ).fetchone()


def save_failed_plan(alarm_id: str, error: str, attempts: int, request_id: str,
                     prompt_version: str, model: str, retrieved_sop_ids: list[str] | None, flags: list[dict]) -> dict:
    """Every try failed: keep a row so the engineer sees it and handles the alarm manually."""
    with get_conn() as conn:
        return conn.execute(
            """
            INSERT INTO handling_plans
                (alarm_id, error, attempts, status, request_id, prompt_version, model, retrieved_sop_ids, flags)
            VALUES (%s, %s, %s, 'generation_failed', %s, %s, %s, %s::text[], %s) RETURNING *
            """,
            (alarm_id, error, attempts, request_id, prompt_version, model, retrieved_sop_ids, Jsonb(flags)),
        ).fetchone()


def save_not_covered_plan(alarm_id: str, plan_json: dict, request_id: str,
                          suggestions: list[dict], flags: list[dict]) -> dict:
    """Rule S1: no SOP covers this alarm. Decided by code, so no Claude call was made:
    attempts 0, prompt_version and model empty, no rows in llm_calls. The plan has no actions.
    status sop_not_covered: it can't be approved; the engineer handles the alarm manually.
    """
    with get_conn() as conn:
        return conn.execute(
            """
            INSERT INTO handling_plans
                (alarm_id, plan_json, attempts, status, request_id, retrieved_sop_ids, sop_suggestions, flags)
            VALUES (%s, %s, 0, 'sop_not_covered', %s, '{}'::text[], %s, %s) RETURNING *
            """,
            (alarm_id, Jsonb(plan_json), request_id, Jsonb(suggestions), Jsonb(flags)),
        ).fetchone()


def save_duplicate(alarm_id: str, request_id: str, flags: list[dict]) -> dict:
    """Rule E1: a duplicate entry. No plan, no Claude call. The row is kept so the page can say which alarm it repeats."""
    with get_conn() as conn:
        return conn.execute(
            """
            INSERT INTO handling_plans (alarm_id, error, attempts, status, request_id, flags)
            VALUES (%s, %s, 0, 'rejected_duplicate', %s, %s) RETURNING *
            """,
            (alarm_id, flags[0]["message"], request_id, Jsonb(flags)),
        ).fetchone()


def find_earlier_duplicate(alarm: dict) -> dict | None:
    """Rule E1: an alarm entered before this one with the same tool_id, chamber, alarm_code, timestamp
    and sensor_value. alarm_id order is entry order in this data; a real system would use the entry time.
    The later entry is the duplicate, so the first one still gets its plan.
    """
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT alarm_id FROM alarm_records
            WHERE tool_id = %(tool_id)s AND chamber = %(chamber)s AND alarm_code = %(alarm_code)s
              AND timestamp = %(timestamp)s AND sensor_value = %(sensor_value)s
              AND alarm_id < %(alarm_id)s
            ORDER BY alarm_id LIMIT 1
            """,
            alarm,
        ).fetchone()


def get_tool_events(tool_id: str, start, end) -> list[dict]:
    """Alarms and repairs on one tool with start <= timestamp < end, oldest first.
    end is the current alarm's time and is excluded, so the alarm never finds itself or anything after it.
    """
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM tool_events
            WHERE tool_id = %s AND timestamp >= %s AND timestamp < %s
            ORDER BY timestamp
            """,
            (tool_id, start, end),
        ).fetchall()


def vector_literal(v: list[float]) -> str:
    """pgvector's text form: '[0.1,0.2,...]'. Passed as a string and cast with ::vector in SQL."""
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


def get_sop_embedding_hashes(model: str) -> dict[str, str]:
    """{sop_id: content_hash} for the SOPs already embedded with this model."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT sop_id, content_hash FROM sop_embeddings WHERE model = %s", (model,)
        ).fetchall()
    return {r["sop_id"]: r["content_hash"] for r in rows}


def upsert_sop_embeddings(rows: list[tuple[str, str, str, str, list[float]]]) -> None:
    """rows: (sop_id, equipment_type, model, content_hash, embedding). A new SOP is inserted, a changed one replaced."""
    with get_conn() as conn:
        for sop_id, equipment_type, model, content_hash, embedding in rows:
            conn.execute(
                """
                INSERT INTO sop_embeddings (sop_id, equipment_type, model, content_hash, embedding)
                VALUES (%s, %s, %s, %s, %s::vector)
                ON CONFLICT (sop_id) DO UPDATE SET
                    equipment_type = EXCLUDED.equipment_type, model = EXCLUDED.model,
                    content_hash = EXCLUDED.content_hash, embedding = EXCLUDED.embedding
                """,
                (sop_id, equipment_type, model, content_hash, vector_literal(embedding)),
            )


def delete_sop_embeddings_except(sop_ids: list[str]) -> None:
    """Remove vectors of SOPs that are no longer in sops.json."""
    with get_conn() as conn:
        conn.execute("DELETE FROM sop_embeddings WHERE NOT (sop_id = ANY(%s::text[]))", (sop_ids,))


def nearest_sop_embeddings(query: list[float], equipment_type: str, model: str, k: int) -> list[dict]:
    """The k SOPs of this equipment type closest to the query vector. <=> is cosine distance: smaller = more alike."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT sop_id, embedding <=> %(q)s::vector AS distance
            FROM sop_embeddings
            WHERE equipment_type = %(equipment_type)s AND model = %(model)s
            ORDER BY embedding <=> %(q)s::vector
            LIMIT %(k)s
            """,
            {"q": vector_literal(query), "equipment_type": equipment_type, "model": model, "k": k},
        ).fetchall()


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
