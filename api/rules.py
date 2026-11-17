"""Design doc section 7 rules, checked by code, not by Claude.

The rules are fixed, so code can't get them wrong; Claude only writes the steps from the SOPs.
Each rule that applies becomes a flag: {"code": "R1", "message": "...", "related": [...]}.

E1  duplicate entry                -> no plan at all (checked first, in main.py)
W1  same event: same tool and alarm code, another chamber, within 30 minutes before this one
W2  reading inside the spec limit
R1  repeat: same tool and alarm code within 7 days before this one, W1 events not counted
S1  no SOP covers this alarm code at this process step (added in main.py, from retrieval)
WF1 wafers were in process
"""

from datetime import timedelta

import db

REPEAT_WINDOW = timedelta(days=7)
SAME_EVENT_WINDOW = timedelta(minutes=30)


def duplicate_flag(alarm: dict) -> dict | None:
    """E1. None if this alarm is not a duplicate of an earlier entry."""
    earlier = db.find_earlier_duplicate(alarm)
    if earlier is None:
        return None
    return {
        "code": "E1",
        "message": f"Duplicate of {earlier['alarm_id']}: same tool, chamber, alarm code, time and reading. "
                   "No plan was generated.",
        "related": [earlier["alarm_id"]],
    }


def not_covered_flag(alarm: dict) -> dict:
    """S1."""
    return {
        "code": "S1",
        "message": f"No SOP covers {alarm['alarm_code']} at {alarm['process_step']}. "
                   "No steps were generated; handle this alarm manually.",
        "related": [],
    }


def check_flags(alarm: dict, code: dict) -> list[dict]:
    """W1, W2, R1, WF1 for one alarm. code = its row in the alarm code table."""
    t = alarm["timestamp"]
    # Every event on this tool in the 7 days before this alarm (this alarm itself is not included).
    events = db.get_tool_events(alarm["tool_id"], t - REPEAT_WINDOW, t)
    same_code = [e for e in events if e["event_type"] == "alarm" and e["alarm_code"] == alarm["alarm_code"]]

    # W1: another chamber, within 30 minutes -> the same event, not a repeat.
    same_event = [e for e in same_code
                  if e["chamber"] != alarm["chamber"] and t - e["timestamp"] <= SAME_EVENT_WINDOW]
    # R1: everything else with the same code in the 7 days.
    repeats = [e for e in same_code if e not in same_event]

    flags = []
    if same_event:
        flags.append({
            "code": "W1",
            "message": "Same event as " + ", ".join(
                f"{e['event_id']} (chamber {e['chamber']}, {int((t - e['timestamp']).total_seconds() // 60)} min earlier)"
                for e in same_event)
                + ": same tool and alarm code in another chamber. Look at them together and check the whole tool.",
            "related": [e["event_id"] for e in same_event],
        })
    if code["spec_lower"] <= alarm["sensor_value"] <= code["spec_upper"]:
        flags.append({
            "code": "W2",
            "message": f"Reading {alarm['sensor_value']:g} {code['unit']} is inside the spec limit "
                       f"({code['spec_lower']:g}-{code['spec_upper']:g} {code['unit']}). The sensor reading may be wrong.",
            "related": [],
        })
    if repeats:
        flags.append({
            "code": "R1",
            "message": f"Repeat alarm: {alarm['tool_id']} reported {alarm['alarm_code']} on "
                       + ", ".join(e["timestamp"].strftime("%Y-%m-%d %H:%M") for e in repeats)
                       + " (within 7 days). Notify the equipment engineer to escalate.",
            "related": [e["event_id"] for e in repeats],
        })
    if alarm["wafer_count"] > 0:
        flags.append({
            "code": "WF1",
            "message": f"{alarm['wafer_count']} wafers were in process during this alarm. Decide release, "
                       "rework or scrap by the fab's wafer disposition procedure.",
            "related": [],
        })
    return flags
