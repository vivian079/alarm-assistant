"""MCP server: the alarm assistant's lookups and plan generation, for any MCP client.

An MCP client (Claude Desktop, Claude Code, another team's agent) sends two kinds of request:
  tools/list  -> the 4 tools below: name, description, inputSchema. The SDK builds them from each
                 function's name, docstring and parameter types, so the definition and the code are one place.
  tools/call  -> run one tool with the arguments the client's model chose, send the text back.
The client runs its own loop with its own model; this program only answers those two requests.

The 3 lookups are the same functions the agent prompt version (handling_plan_v7) calls, in tools.py.
Plan generation goes through the API's own endpoint, so rules, retrieval, logging and saving are the same
as when the page asks for a plan. This program never calls Claude itself and needs no API key.

Transport: stdio. The client starts this program (docker compose run --rm -T mcp, see README) and talks to
it over stdin/stdout; nothing listens on the network. stdout carries only MCP messages (the SDK logs to stderr).

Not a tool, on purpose: approving a plan. Whether a tool gets called is the model's decision, and approval
has to be the engineer's, so it stays a button on the page.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

import db
import tools

API_URL = os.environ.get("API_URL", "http://api:8000")
UI_URL = "http://localhost:8501"
# An agent prompt version can make up to 5 API calls for one plan (llm.MAX_TURNS); same limit as run_eval.
PLAN_TIMEOUT_S = 5 * 180

server = MCPServer(
    "alarm-assistant",
    instructions=(
        "Semiconductor equipment alarms: a tool's alarm and repair history, the same alarm code on other tools, "
        "the SOPs for an alarm code, and handling plans for alarm records. A plan is a draft until an equipment "
        "engineer approves it on the web page; no tool here approves plans."
    ),
)

# Hints for the client, e.g. whether it should ask the user before calling. They describe the tool; they don't enforce anything.
READ_ONLY = ToolAnnotations(read_only_hint=True)
# Adds a plan row and calls Claude through the API (costs money) on every call, but deletes or changes nothing.
CREATES_PLAN = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)

AlarmId = Annotated[
    str | None,
    Field(description="Optional alarm record ID, e.g. AL-017. Given: look at the days before that alarm. "
                      "Not given: look at the days before now."),
]
Days = Annotated[int, Field(description=f"How many days back to look, 1 to {tools.MAX_DAYS}.")]


def _window_end(alarm_id: str | None) -> dict:
    """tools.py ends every lookup at the timestamp of the alarm it is given (in the agent loop, the alarm being handled).
    With an alarm_id, code reads that alarm's time from the table: the model only copies an ID and never writes a date.
    (A wrong date would return wrong rows with no error; a wrong ID is an error the model and the engineer can see.)
    Without one, the window ends now (this container's clock, UTC), for questions like "the last 30 days".
    """
    if alarm_id is None:
        return {"timestamp": datetime.now()}
    alarm = db.get_alarm(alarm_id)
    if alarm is None:
        raise ToolError(f"Alarm {alarm_id} not found.")
    return alarm


def _run(name: str, alarm: dict, **tool_input) -> str:
    """Same entry point as the agent loop (tools.run). A bad input (days=500) comes back as an error
    result (isError) the model reads and can correct, not as a crash."""
    text, is_error = tools.run(name, tool_input, alarm)
    if is_error:
        raise ToolError(text)
    return text


@server.tool(annotations=READ_ONLY)
def get_tool_history(
    tool_id: Annotated[str, Field(description="Equipment ID, e.g. ETCH-03.")],
    days: Days,
    alarm_id: AlarmId = None,
) -> str:
    """Alarms and repairs on one tool (all chambers), oldest first."""
    return _run("get_tool_history", _window_end(alarm_id), tool_id=tool_id, days=days)


@server.tool(annotations=READ_ONLY)
def get_alarm_code_history(
    alarm_code: Annotated[str, Field(description="Alarm code, e.g. A-1023.")],
    days: Days,
    alarm_id: AlarmId = None,
) -> str:
    """Every tool that reported this alarm code, oldest first."""
    return _run("get_alarm_code_history", _window_end(alarm_id), alarm_code=alarm_code, days=days)


@server.tool(annotations=READ_ONLY)
def search_sops(alarm_code: Annotated[str, Field(description="Alarm code, e.g. A-1023.")]) -> str:
    """Every SOP that lists this alarm code (any process step), with its numbered steps."""
    return _run("search_sops", {}, alarm_code=alarm_code)


@server.tool(annotations=CREATES_PLAN)
def generate_handling_plan(alarm_id: Annotated[str, Field(description="Alarm record ID, e.g. AL-017.")]) -> str:
    """Generate a handling plan for one alarm record, the same way the web page does: code checks the rules (flags)
    and finds the SOPs, Claude writes the steps, and the plan is saved with status pending_review.
    Every call makes a new plan and costs one generation. The plan is a draft until an engineer approves it on the page."""
    url = f"{API_URL}/alarms/{urllib.parse.quote(alarm_id, safe='')}/plan"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="POST"), timeout=PLAN_TIMEOUT_S) as resp:
            row = json.load(resp)
    except urllib.error.HTTPError as e:  # 404 unknown alarm, 422 alarm code not in the table
        raise ToolError(f"The API refused alarm {alarm_id}: HTTP {e.code} {e.read().decode()}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise ToolError(f"Could not reach the API at {API_URL} ({e}). Start it with: docker compose up -d") from None
    return format_plan(row)


def format_plan(row: dict) -> str:
    """The saved handling_plans row as text for the model: what the page shows, without tokens and log fields."""
    lines = [f"plan_id {row['plan_id']}, alarm {row['alarm_id']}, status {row['status']}"]
    lines += [f"flag {f['code']}: {f['message']}" for f in row.get("flags") or []]
    status = row["status"]
    if status == "rejected_duplicate":
        lines.append("No plan: this record is a duplicate entry (rule E1).")
        return "\n".join(lines)
    if status == "generation_failed":
        lines.append(f"No plan: generation failed ({row['error']}). The engineer handles this alarm by the SOP manually.")
        return "\n".join(lines)

    plan = row["plan_json"]
    s = plan["alarm_summary"]
    lines.append(f"summary: {s['tool_id']} chamber {s['chamber']}, {s['alarm_code']}, reading {s['sensor_value']} "
                 f"(spec {s['spec_lower']}-{s['spec_upper']} {s['unit']}), {s['wafer_count']} wafers in process")
    if status == "sop_not_covered":
        lines.append("No SOP covers this alarm code at this process step (rule S1), so the plan has no steps. "
                     "SOPs that may be related (not confirmed, for the engineer to check):")
        lines += [f"  {x['sop_id']} {x['title']} (similarity {x['similarity']})" for x in row.get("sop_suggestions") or []]
        return "\n".join(lines)

    if plan["likely_causes"]:
        lines.append("likely causes: " + "; ".join(plan["likely_causes"]))
    lines.append("actions:")
    for i, a in enumerate(plan["actions"], 1):
        refs = ", ".join(f"{r['sop_id']} step {r['step']}" for r in a["sop_refs"]) or "no SOP source"
        lines.append(f"  {i}. {a['step']} [{refs}]")
    lines.append(f"This is a draft. An engineer reviews and approves it on the web page ({UI_URL}).")
    return "\n".join(lines)


if __name__ == "__main__":
    server.run()  # stdio
