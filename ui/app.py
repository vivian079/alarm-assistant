import os

import requests
import streamlit as st

API_URL = os.environ.get("API_URL", "http://localhost:8000")

st.title("Alarm Handling Assistant")

alarms = requests.get(f"{API_URL}/alarms", timeout=10).json()
alarm_id = st.selectbox("Alarm", [a["alarm_id"] for a in alarms])
alarm = next(a for a in alarms if a["alarm_id"] == alarm_id)

st.subheader("Alarm record")
st.json(alarm)

if st.button("Generate handling plan"):
    with st.spinner("Waiting for Claude..."):
        resp = requests.post(f"{API_URL}/alarms/{alarm_id}/plan", timeout=180)
    resp.raise_for_status()
    st.session_state["plan"] = resp.json()

plan = st.session_state.get("plan")
if plan and plan["alarm_id"] == alarm_id:
    st.subheader("Handling plan")
    st.caption(f"plan_id {plan['plan_id']} · status {plan['status']} · {plan['created_at']}")
    st.markdown(plan["plan_text"])
    if plan["status"] == "pending_review" and st.button("Approve"):
        resp = requests.post(f"{API_URL}/plans/{plan['plan_id']}/approve", timeout=10)
        resp.raise_for_status()
        st.session_state["plan"] = resp.json()
        st.rerun()
