# Handling plan prompt versions

Each version is a folder with `system.txt` and `user.txt`.
`PROMPT_VERSION` in `.env` says which folder the API uses.

Rules:
- A version is never edited after it has been used. A change is a new folder (`handling_plan_v3`, ...).
- `{sop_library}` in `system.txt` is replaced with all SOPs from `data/sops.json`. A version without it sends no SOPs.
- `user.txt` is filled with the alarm record and its spec limit (`{alarm_id}`, `{tool_id}`, ...).

| version | what changed |
|---|---|
| handling_plan_v1 | first structured-output prompt; no SOPs in the prompt (baseline) |
| handling_plan_v2 | adds the whole SOP library (40 SOPs) to the system prompt |
