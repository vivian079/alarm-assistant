import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

import db
import llm


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Alarm Handling Assistant", lifespan=lifespan)


@app.get("/alarms")
def list_alarms():
    """For the page's dropdown."""
    return db.list_alarms()


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

    try:
        plan, attempts = llm.generate_plan(prompt_input, request_id)   # 3. call Claude with the schema, retry on failure
    except llm.PlanGenerationError as e:
        return db.save_failed_plan(
            alarm_id, str(e), e.attempts, request_id, llm.PROMPT_VERSION, llm.MODEL)   # 4a. every try failed: status = generation_failed
    # 4b. save (status = pending_review), return. The returned row has prompt_version and model,
    # so whoever called this endpoint (the page, run_eval.py) knows what made the plan.
    return db.save_plan(alarm_id, plan.model_dump(), attempts, request_id, llm.PROMPT_VERSION, llm.MODEL)


@app.post("/plans/{plan_id}/approve")
def approve_plan(plan_id: int):
    plan = db.approve_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan {plan_id} not found or not pending review")
    return plan
