# Alarm Handling Assistant

Input one semiconductor equipment alarm record, get a handling plan that an equipment engineer reviews.
All data in `data/` is synthetic.

Current stage: structured output + evaluation. One synchronous endpoint. The plan follows a Pydantic schema
(`api/schemas.py`) through the API's Structured Outputs; if the output fails validation, the API retries
with the error message, up to 2 times, then saves the plan as `generation_failed`.
The prompt gets the alarm record, its spec limit from the alarm code table, and the whole SOP library
(all 40 SOPs in the system prompt; no retrieval yet).

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

## Evaluation (`eval/`)

`golden_sets/golden_v1.json` holds 20 alarms with hand-written expected SOP steps, including the hard cases:
the same alarm code needing a different SOP in a different process step, a reading inside the spec limit,
an alarm no SOP covers (expected: no steps), and repeat alarms.

`run_eval.py` sends each golden alarm through the API and scores the plan:
- actions: precision, recall, F1 against the expected SOP steps. Code checks the SOP references;
  an LLM judge (`judge.py`) checks that each action's text does what the cited SOP step says.
- alarms no SOP covers: right only if the plan has no steps.
- alarm_summary: each value compared with the data tables.

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
