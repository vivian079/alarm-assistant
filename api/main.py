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
    alarm = db.get_alarm(alarm_id)                  # 1. read the record from Postgres
    if alarm is None:
        raise HTTPException(status_code=404, detail=f"alarm {alarm_id} not found")
    plan_text = llm.generate_plan(alarm)            # 2. fill prompt, call Claude, wait
    return db.save_plan(alarm_id, plan_text)        # 3. save (status = pending_review), return


@app.post("/plans/{plan_id}/approve")
def approve_plan(plan_id: int):
    plan = db.approve_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan {plan_id} not found")
    return plan
