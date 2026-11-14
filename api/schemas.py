"""The handling plan format.

The same classes are used twice:
1. HandlingPlan -> JSON schema -> sent to the API (Structured Outputs), so Claude's output must follow it.
2. Claude's JSON -> HandlingPlan object, so code reads fields by name (plan.actions[0].sop_refs[0].sop_id).

flags and status are not here: flags come from code rules, status is set when the plan is saved.
"""

from pydantic import BaseModel


class SopRef(BaseModel):
    sop_id: str  # e.g. "SOP-012"
    step: int    # step number inside that SOP


class Action(BaseModel):
    # Can be empty on purpose: a step with no SOP source shows up as [] and code can flag it.
    sop_refs: list[SopRef]
    step: str    # what to do in this step


class AlarmSummary(BaseModel):
    # Same names as the alarm record and alarm code table, so values can be compared field by field.
    tool_id: str
    chamber: str
    alarm_code: str
    sensor_value: float
    spec_lower: float
    spec_upper: float
    unit: str
    wafer_count: int
    recent_alarm: str  # what this tool recently alarmed on
    recent_fix: str    # what was recently repaired


class HandlingPlan(BaseModel):
    alarm_summary: AlarmSummary
    likely_causes: list[str]
    actions: list[Action]
