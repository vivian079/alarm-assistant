# Alarm Handling Assistant

Input one semiconductor equipment alarm record, get a handling plan that an equipment engineer reviews.
All data in `data/` is synthetic.

Current stage: rules checked by code, retrieval, structured output, evaluation. One synchronous endpoint. The plan follows a Pydantic schema
(`api/schemas.py`) through the API's Structured Outputs; if the output fails validation, the API retries
with the error message, up to 2 times, then saves the plan as `generation_failed`.
The prompt gets the alarm record, its spec limit from the alarm code table, and SOPs: the whole library
(prompt versions v2, v3) or only the SOPs retrieved for this alarm (v4).

## Rules checked by code (`api/rules.py`)

The design doc's rules are fixed, so code checks them, not Claude. Each one that applies is a flag on the plan
(`handling_plans.flags`), shown at the top of the page:

- E1 duplicate entry (same tool, chamber, alarm code, time and reading as an earlier entry): no plan, status `rejected_duplicate`
- W1 same event: same tool and alarm code in another chamber within the 30 minutes before
- W2 reading inside the spec limit (the sensor may be wrong)
- R1 repeat alarm: same tool and alarm code within the 7 days before (W1 events not counted): escalate
- S1 no SOP covers this alarm code at this process step: no steps (see Retrieval)
- WF1 wafers were in process: decide their disposition

R1 and W1 read the table `tool_events`: one row per alarm or repair on a tool (every alarm record, plus the
older alarms and repairs in `data/tool_events.json`). Code never parses the free-text `recent_history`;
that text only goes into the prompt.

## Retrieval (`api/retrieval.py`)

1. Exact match: SOPs whose `alarm_codes` contain the alarm code and whose `process_steps` contain the
   process step. Only these go into the prompt.
2. No match = rule S1 (no SOP covers this case). Code decides it: Claude is not called, the plan has no steps,
   status `sop_not_covered`.
3. For S1 only, the page lists the 3 SOPs whose text (title + steps) is closest to the alarm code's description,
   marked "not confirmed". They help the engineer find an SOP whose tags are missing. They never go into a prompt,
   because a similar SOP is not necessarily the right one.

Embeddings: `BAAI/bge-small-en-v1.5` through fastembed, run inside the API container (no API key;
the model is downloaded when the image is built). One vector per SOP, stored in Postgres with pgvector
(table `sop_embeddings`), refreshed on API startup for new or changed SOPs.
`handling_plans.retrieved_sop_ids` records which SOPs went into the prompt; `run_eval.py` checks them
against the golden set separately from the plan.

## Prompt versions

Prompts are plain text files, one folder per version (`api/prompts/`, `eval/judge_prompts/`);
golden sets are one file per version (`eval/golden_sets/`). A version is never edited after it has been used:
a change is a new version. `.env` picks which one runs (`PROMPT_VERSION`, `JUDGE_PROMPT_VERSION`,
`GOLDEN_SET_VERSION`), so switching or rolling back is a one-line change with no code change.

## Audit log

Every Claude API call the API makes is one row in the Postgres table `llm_calls`, failed calls included:
prompt version, model, the full system prompt and messages sent, the raw output, stop reason,
input and output tokens, latency, and the error if the call did not give a plan.
Rows are only inserted, never updated or deleted. `handling_plans.request_id` links a plan to its calls.

```
docker compose exec db psql -U alarm -d alarm
SELECT call_id, alarm_id, attempt, prompt_version, stop_reason, input_tokens, output_tokens, latency_ms, error
FROM llm_calls ORDER BY call_id DESC LIMIT 10;
```

## Data (`data/`, synthetic)

- `equipment.json`: 8 tools (etch, deposition, CMP)
- `alarm_codes.json`: 30 alarm codes, each with a spec limit
- `sops.json`: 40 SOPs; each applies to some alarm codes and process steps and has numbered steps
- `alarm_records.json`: 21 alarm records
- `tool_events.json`: older alarms and repairs that have no alarm record (for rules R1 and W1)

## Evaluation (`eval/`)

`golden_sets/golden_v1.json` holds 20 alarms with hand-written expected SOP steps, including the hard cases:
the same alarm code needing a different SOP in a different process step, a reading inside the spec limit,
an alarm no SOP covers (expected: no steps), and repeat alarms. `golden_v2.json` adds the expected flags
for each alarm and a duplicate entry (AL-051).

`run_eval.py` sends each golden alarm through the API and scores the plan:
- actions: precision, recall, F1 against the expected SOP steps. Code checks the SOP references;
  an LLM judge (`judge.py`) checks that each action's text does what the cited SOP step says.
- alarms no SOP covers: right only if the plan has no steps.
- alarm_summary: each value compared with the data tables.
- flags (golden_v2): the plan's flags compared with the expected ones, per alarm and per flag.

Results are saved in `eval/results/`. Each file records the prompt version and model (from the API's
response), the judge prompt version, judge model and golden set version, and has the versions in its name.
`eval/results/old_runs.md` lists what the first four runs used.

## Run

```
cp .env.example .env        # then put your ANTHROPIC_API_KEY in .env
docker compose up --build
```

After changing a version in `.env`, restart the API so it reads the new value: `docker compose up -d --build api`.

- Page: http://localhost:8501
- API docs: http://localhost:8000/docs

Run the evaluation (with the services up):

```
docker compose run --rm eval
```

Run one alarm several times, to see how much its result changes from run to run (each run makes a new plan):

```
docker compose run --rm eval python run_eval.py --only AL-063 --repeat 5
```
