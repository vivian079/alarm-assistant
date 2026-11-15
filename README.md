# Alarm Handling Assistant

Input one semiconductor equipment alarm record, get a handling plan that an equipment engineer reviews.
All data in `data/` is synthetic.

Current stage: structured output + evaluation. One synchronous endpoint. The plan follows a Pydantic schema
(`api/schemas.py`) through the API's Structured Outputs; if the output fails validation, the API retries
with the error message, up to 2 times, then saves the plan as `generation_failed`.
The prompt gets the alarm record, its spec limit from the alarm code table, and the whole SOP library
(all 40 SOPs in the system prompt; no retrieval yet).

## Data (`data/`, synthetic)

- `equipment.json`: 8 tools (etch, deposition, CMP)
- `alarm_codes.json`: 30 alarm codes, each with a spec limit
- `sops.json`: 40 SOPs; each applies to some alarm codes and process steps and has numbered steps
- `alarm_records.json`: 21 alarm records

## Evaluation (`eval/`)

`golden_set.json` holds 20 alarms with hand-written expected SOP steps, including the hard cases:
the same alarm code needing a different SOP in a different process step, a reading inside the spec limit,
an alarm no SOP covers (expected: no steps), and repeat alarms.

`run_eval.py` sends each golden alarm through the API and scores the plan:
- actions: precision, recall, F1 against the expected SOP steps. Code checks the SOP references;
  an LLM judge (`judge.py`) checks that each action's text does what the cited SOP step says.
- alarms no SOP covers: right only if the plan has no steps.
- alarm_summary: each value compared with the data tables.

Results are saved in `eval/results/`.

## Run

```
cp .env.example .env        # then put your ANTHROPIC_API_KEY in .env
docker compose up --build
```

- Page: http://localhost:8501
- API docs: http://localhost:8000/docs

Run the evaluation (with the services up):

```
docker compose run --rm eval
```
