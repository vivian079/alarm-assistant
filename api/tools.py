"""The functions Claude can call in the agent prompt versions (handling_plan_v7 on).

Claude never runs anything itself. When it wants information, it answers with a tool_use block:
the tool's name and its input, in the format given in prompts/<version>/tools.json.
llm.py then calls run() below, and sends the returned text back to Claude as a tool_result.

Every lookup is about the past of the alarm being handled: the time window always ends at that
alarm's timestamp (taken from the alarm, not from Claude), so Claude can't see later events.
"""

from datetime import timedelta

import db
import retrieval

MAX_DAYS = 90


def _check_days(days: int) -> None:
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_DAYS}, got {days}")


def _format_events(rows: list[dict]) -> str:
    """One line per event, oldest first."""
    lines = []
    for e in rows:
        when = e["timestamp"].strftime("%Y-%m-%d %H:%M")
        what = f"alarm {e['alarm_code']}" if e["event_type"] == "alarm" else f"repair: {e['note']}"
        lines.append(f"{when}  {e['tool_id']} chamber {e['chamber']}  {what}")
    return "\n".join(lines)


def get_tool_history(alarm: dict, tool_id: str, days: int) -> str:
    """Alarms and repairs on one tool in the `days` before this alarm."""
    _check_days(days)
    end = alarm["timestamp"]
    start = end - timedelta(days=days)
    rows = db.get_tool_events(tool_id, start, end)
    if not rows:
        return f"No alarms or repairs on {tool_id} from {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M}."
    return _format_events(rows)


def get_alarm_code_history(alarm: dict, alarm_code: str, days: int) -> str:
    """Every tool that reported this alarm code in the `days` before this alarm."""
    _check_days(days)
    end = alarm["timestamp"]
    start = end - timedelta(days=days)
    rows = db.get_alarm_code_events(alarm_code, start, end)
    if not rows:
        return f"No tool reported {alarm_code} from {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M}."
    return _format_events(rows)


def search_sops(alarm: dict, alarm_code: str) -> str:
    """Every SOP that lists this alarm code (any process step), with its steps."""
    found = [s for s in retrieval.SOPS if alarm_code in s["alarm_codes"]]
    if not found:
        return f"No SOP lists {alarm_code}."
    return retrieval.format_sops(found)


FUNCTIONS = {
    "get_tool_history": get_tool_history,
    "get_alarm_code_history": get_alarm_code_history,
    "search_sops": search_sops,
}


def run(name: str, tool_input: dict, alarm: dict) -> tuple[str, bool]:
    """Run one tool call. Returns (text for the tool_result, is_error).
    A bad input is sent back to Claude as an error result instead of stopping the plan,
    so Claude can correct the input on its next turn.
    """
    func = FUNCTIONS.get(name)
    if func is None:
        return f"Unknown tool {name}", True
    try:
        return func(alarm, **tool_input), False
    except (TypeError, ValueError) as e:
        return f"Bad input for {name}: {e}", True
