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
    sop_ids = plan.get("retrieved_sop_ids")
    st.caption(
        f"plan_id {plan['plan_id']} · status {plan['status']} · "
        f"attempts {plan['attempts']} · prompt {plan.get('prompt_version')} · "
        + (f"SOPs in prompt {', '.join(sop_ids) or 'none'} · " if sop_ids is not None else "")
        + f"{plan['created_at']}"
    )

    # Flags come from rules checked by code (design doc section 7), not from Claude.
    for flag in plan.get("flags") or []:
        show = st.info if flag["code"] == "WF1" else st.warning
        show(f"{flag['code']}: {flag['message']}")

    if plan["status"] == "rejected_duplicate":
        st.error("This alarm was entered twice. No plan was generated; use the plan of the first entry.")
    elif plan["status"] == "generation_failed":
        st.error(
            f"Generation failed after {plan['attempts']} tries. "
            f"Handle this alarm manually with the SOP.\n\n{plan['error']}"
        )
    else:
        p = plan["plan_json"]

        if plan["status"] == "sop_not_covered":
            # Rule S1, decided by code: no Claude call, no steps (the S1 flag above says so).
            if plan.get("sop_suggestions"):
                st.markdown("**SOPs that may be related** (found by text similarity, not confirmed: check before using)")
                for s in plan["sop_suggestions"]:
                    st.markdown(f"- {s['sop_id']} {s['title']} (similarity {s['similarity']:.2f})")

        st.markdown("**Alarm summary**")
        st.table([{"field": k, "value": str(p["alarm_summary"][k])} for k in SUMMARY_FIELDS])

        if plan["status"] == "sop_not_covered":
            st.stop()

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
