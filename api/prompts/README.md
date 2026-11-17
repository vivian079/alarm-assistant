# Handling plan prompt versions

Each version is a folder with `system.txt` and `user.txt`.
`PROMPT_VERSION` in `.env` says which folder the API uses.

Rules:
- A version is never edited after it has been used. A change is a new folder (`handling_plan_v3`, ...).
- `{sop_library}` in `system.txt` is replaced with all SOPs from `data/sops.json`. A version without it sends no SOPs.
- `user.txt` is filled with the alarm record and its spec limit (`{alarm_id}`, `{tool_id}`, ...).
- `{retrieved_sops}` in `user.txt` turns on retrieval: only the SOPs that exact match finds for this alarm
  (its alarm code and process step) go there. If none match, rule S1 applies: the code saves the plan as
  `sop_not_covered` with no steps and does not call Claude.

| version | what changed |
|---|---|
| handling_plan_v1 | first structured-output prompt; no SOPs in the prompt (baseline) |
| handling_plan_v2 | adds the whole SOP library (40 SOPs) to the system prompt |
| handling_plan_v3 | same as v2, plus: the repeat-alarm escalation is added by the system, so it is not written as an action |
| handling_plan_v4 | same instructions as v3, but no SOP library in the system prompt: the SOPs exact match finds for this alarm go into the user message (`{retrieved_sops}`) |
| handling_plan_v5 | v4, plus: an action that cites an SOP step says what the step says (an extra operation is its own action with empty sop_refs); the wafer reminder is added by the system (rule WF1), so wafer hold / review / disposition is not written as an action |

The escalation (rule R1) and the wafer reminder (rule WF1) are flags added by code (`api/rules.py`),
for every prompt version. v3 and v4 tell Claude the escalation is added by the system; v5 says the same for the wafer reminder.
