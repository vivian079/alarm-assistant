import os

import requests
import streamlit as st

API_URL = os.environ.get("API_URL", "http://localhost:8000")
# The API can call Claude up to 3 times (first try + 2 retries), each up to ~180 s.
PLAN_TIMEOUT = 3 * 180
# Postgres JSONB does not keep key order, so show the summary in the schema's order.
SUMMARY_FIELDS = [
    "tool_id", "chamber", "alarm_code", "sensor_value", "spec_lower", "spec_upper",
    "unit", "wafer_count", "recent_alarm", "recent_fix",
]

st.title("Alarm Handling Assistant")

alarms = requests.get(f"{API_URL}/alarms", timeout=10).json()
alarm_id = st.selectbox("Alarm", [a["alarm_id"] for a in alarms])
alarm = next(a for a in alarms if a["alarm_id"] == alarm_id)

st.subheader("Alarm record")
st.json(alarm)

if st.button("Generate handling plan"):
    with st.spinner("Waiting for Claude..."):
        resp = requests.post(f"{API_URL}/alarms/{alarm_id}/plan", timeout=PLAN_TIMEOUT)
    resp.raise_for_status()
    st.session_state["plan"] = resp.json()

plan = st.session_state.get("plan")
if plan and plan["alarm_id"] == alarm_id:
    st.subheader("Handling plan")
    st.caption(
        f"plan_id {plan['plan_id']} · status {plan['status']} · "
        f"attempts {plan['attempts']} · {plan['created_at']}"
    )

    if plan["status"] == "generation_failed":
        st.error(
            f"Generation failed after {plan['attempts']} tries. "
            f"Handle this alarm manually with the SOP.\n\n{plan['error']}"
        )
    else:
        p = plan["plan_json"]

        st.markdown("**Alarm summary**")
        st.table([{"field": k, "value": str(p["alarm_summary"][k])} for k in SUMMARY_FIELDS])

        st.markdown("**Likely causes**")
        for cause in p["likely_causes"]:
            st.markdown(f"- {cause}")

        st.markdown("**Actions**")
        for i, action in enumerate(p["actions"], start=1):
            if action["sop_refs"]:
                refs = ", ".join(f"{r['sop_id']} step {r['step']}" for r in action["sop_refs"])
                st.markdown(f"{i}. {action['step']}  \n`{refs}`")
            else:
                # Empty sop_refs: this step has no SOP source, so show it as a warning.
                st.warning(f"{i}. {action['step']}  \nNo SOP source")

        if plan["status"] == "pending_review" and st.button("Approve"):
            resp = requests.post(f"{API_URL}/plans/{plan['plan_id']}/approve", timeout=10)
            resp.raise_for_status()
            st.session_state["plan"] = resp.json()
            st.rerun()
