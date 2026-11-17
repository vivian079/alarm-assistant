import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

import db
import llm
import retrieval
import rules
from schemas import AlarmSummary, HandlingPlan


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    retrieval.index_sops()  # embed new or changed SOPs into sop_embeddings
    yield


app = FastAPI(title="Alarm Handling Assistant", lifespan=lifespan)


@app.get("/alarms")
def list_alarms():
    """For the page's dropdown."""
    return db.list_alarms()


def not_covered_plan(prompt_input: dict) -> HandlingPlan:
    """The plan for rule S1, made by code: the summary comes straight from the data tables, no causes, no actions.
    recent_history is one free-text field, so code doesn't split it into recent_alarm and recent_fix.
    """
    summary = AlarmSummary(
        **{k: prompt_input[k] for k in
           ("tool_id", "chamber", "alarm_code", "sensor_value", "spec_lower", "spec_upper", "unit", "wafer_count")},
        recent_alarm=prompt_input["recent_history"] or "none in the last 7 days",
        recent_fix="",
    )
    return HandlingPlan(alarm_summary=summary, likely_causes=[], actions=[])


@app.post("/alarms/{alarm_id}/plan")
def create_plan(alarm_id: str):
    """The main endpoint: one alarm in, one handling plan out."""
    alarm = db.get_alarm(alarm_id)                          # 1. read the record from Postgres
    if alarm is None:
        raise HTTPException(status_code=404, detail=f"alarm {alarm_id} not found")

    code = db.get_alarm_code(alarm["alarm_code"])           # 2. look up the spec limit by alarm_code
    if code is None:
        raise HTTPException(
            status_code=422,
            detail=f"alarm_code {alarm['alarm_code']} is not in the alarm code table",
        )
    prompt_input = {
        **alarm,
        "spec_lower": code["spec_lower"],
        "spec_upper": code["spec_upper"],
        "unit": code["unit"],
    }

    # One id for this generation: the plan row and every llm_calls row it made share it.
    request_id = uuid.uuid4().hex

    # 3. Rules checked by code (design doc section 7). E1 stops here: a duplicate gets no plan.
    duplicate = rules.duplicate_flag(alarm)
    if duplicate is not None:
        return db.save_duplicate(alarm_id, request_id, [duplicate])
    flags = rules.check_flags(alarm, code)

    retrieved_sop_ids = None
    if llm.USES_RETRIEVAL:
        # 4. Find the SOPs by exact match on alarm code + process step.
        sops = retrieval.find_sops(alarm["alarm_code"], code["equipment_type"], alarm["process_step"])
        if not sops:
            # 4a. Rule S1: no SOP covers this case. Code decides it, so Claude is not called and there are no steps.
            # The SOPs closest in meaning are listed for the engineer to check; they are not sent to Claude.
            suggestions = retrieval.suggest_sops(code["description"], code["equipment_type"])
            return db.save_not_covered_plan(
                alarm_id, not_covered_plan(prompt_input).model_dump(), request_id, suggestions,
                [rules.not_covered_flag(alarm)] + flags)
        # 4b. Only these SOPs go into the prompt.
        prompt_input["retrieved_sops"] = retrieval.format_sops(sops)
        retrieved_sop_ids = [s["sop_id"] for s in sops]

    try:
        plan, attempts = llm.generate_plan(prompt_input, request_id)   # 5. call Claude with the schema, retry on failure
    except llm.PlanGenerationError as e:
        return db.save_failed_plan(                                    # 6a. every try failed: status = generation_failed
            alarm_id, str(e), e.attempts, request_id, llm.PROMPT_VERSION, llm.MODEL, retrieved_sop_ids, flags)
    # 6b. save (status = pending_review), return. The returned row has prompt_version, model, retrieved_sop_ids
    # and flags, so whoever called this endpoint (the page, run_eval.py) knows what made the plan.
    return db.save_plan(alarm_id, plan.model_dump(), attempts, request_id,
                        llm.PROMPT_VERSION, llm.MODEL, retrieved_sop_ids, flags)


@app.post("/plans/{plan_id}/approve")
def approve_plan(plan_id: int):
    plan = db.approve_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan {plan_id} not found or not pending review")
    return plan
