# Alarm Handling Assistant

Input one semiconductor equipment alarm record, get a handling plan that an equipment engineer reviews.
All data in `data/` is synthetic.

Current stage: rules checked by code, retrieval, structured output, evaluation, an agent prompt version
that can call tools, and an MCP server so Claude Desktop or Claude Code can use the same lookups. One synchronous endpoint. The plan follows a Pydantic schema
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

## Agent version (`handling_plan_v7`, `api/tools.py`)

The other versions put everything into the prompt before calling Claude. v7 sends the same prompt (alarm record,
retrieved SOPs, flags as system notes) plus three tools Claude can call when it wants more:

- `get_tool_history(tool_id, days)`: alarms and repairs on a tool, further back than `recent_history`'s 7 days
- `get_alarm_code_history(alarm_code, days)`: the same alarm code on other tools
- `search_sops(alarm_code)`: the SOPs of another alarm code (background only; the plan cites only the retrieved SOPs)

The loop (`llm.generate_plan_with_tools`) is a plain while loop: call Claude; if it stops with `tool_use`, run the
tools it asked for, send its turn and the results back, call again; stop when it returns the plan. At most 5 API calls
per plan (`MAX_TURNS`); after that the plan is saved as `generation_failed` for the engineer to handle.
Lookups always end at the alarm's own timestamp. What stays in code: the rules (flags), S1, and which SOPs are cited.
Each turn is one row in `llm_calls`, with the whole output in `response_content` (a turn that only asks for a tool
has no text). `handling_plans` keeps the plan's tokens and the tools it called.


## MCP server (`api/mcp_server.py`)

The same lookups, plus plan generation, for any MCP client (Claude Desktop, Claude Code, another team's agent),
so each client doesn't need its own copy of the tool definitions and calling code.

| Tool | What it does |
|---|---|
| `get_tool_history(tool_id, days, alarm_id?)` | alarms and repairs on a tool |
| `get_alarm_code_history(alarm_code, days, alarm_id?)` | the same alarm code on any tool |
| `search_sops(alarm_code)` | the SOPs that list an alarm code |
| `generate_handling_plan(alarm_id)` | calls `POST /alarms/{id}/plan`; the plan is saved as `pending_review` |

- The lookups are the functions in `api/tools.py` that the agent version uses. The window ends at the alarm given
  as `alarm_id` (code reads its timestamp; the model never writes a date) or, without one, now.
- No approve tool: whether a tool is called is the model's decision, and approval is the engineer's, on the page.
- Transport: stdio. The client starts the server with `docker compose run --rm -T mcp`; nothing listens on the network.
  A team-wide deployment would use Streamable HTTP with sign-in (OAuth) instead.
- Known duplication: the agent version's tool definitions (`api/prompts/handling_plan_v7/tools.json`) are a second,
  hand-written copy of the same lookups (no `alarm_id`: there the window always ends at the alarm being handled).
  The function code is shared (`tools.py`), but a new parameter has to be added in both places. Left as is because
  the version in use (v6) calls no tools; if the agent version is used, its loop should get the tools from this
  server (tools/list, tools/call) instead.

Build it once, then add it to the client.

```
docker compose --profile mcp build mcp
```

Claude Desktop: add this to `~/Library/Application Support/Claude/claude_desktop_config.json` (create it if missing),
with the full path of `docker` (`which docker`) and of this repo's `docker-compose.yml`. Docker Desktop must be running.
Then quit and reopen Claude Desktop:

```json
{
  "mcpServers": {
    "alarm-assistant": {
      "command": "/usr/local/bin/docker",
      "args": ["compose", "-f", "/Users/<you>/Documents/alarm-assistant/docker-compose.yml", "run", "--rm", "-T", "mcp"]
    }
  }
}
```

Claude Code: `claude mcp add alarm-assistant -- docker compose -f <repo>/docker-compose.yml run --rm -T mcp`

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

- `equipment.json`: 11 tools (etch, deposition, CMP)
- `alarm_codes.json`: 30 alarm codes, each with a spec limit
- `sops.json`: 40 SOPs; each applies to some alarm codes and process steps and has numbered steps
- `alarm_records.json`: 41 alarm records (AL-017 to AL-075 for the golden set, AL-076 to AL-095 for the held-out set)
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
- cost: API calls, tokens and estimated cost of the plans, and which tools were called.

`golden_sets/heldout_v1.json` is a held-out set: 20 alarms (AL-076 to AL-095) whose results are never used to
change a prompt, so its numbers show how a prompt does on alarms it was not tuned on. The golden set stopped being
that once v3 to v6 were each written from its mistakes.

Results are saved in `eval/results/`. Each file records the prompt version and model (from the API's
response), the judge prompt version, judge model and golden set version, and has the versions in its name.
`eval/results/old_runs.md` lists what the first four runs used.

### Checking the judge (`eval/judge_check.py`)

P depends on the judge: an action counts as right only when the judge says its text does what the cited SOP step
says. Checking only the steps the judge rejected finds a judge that is too strict, never one that is too lenient,
and from handling_plan_v6 on the judge rejects nothing. `eval/judge_sets/judge_check_v1.json` has labelled steps
of both kinds:
- 20 wrong steps: a real expected SOP step is cited, but the text was changed (7 with an added operation,
  7 with a different operation, 6 with part of the step left out). The reference is right, so the code check
  passes and only the judge can catch them.
- 20 right-step candidates: real actions from earlier prompt versions whose text was reworded from the SOP, picked
  at random from the ones the judge accepted. They need a person's label and are not counted until they have one
  (not labelled yet).

The two groups are reported separately. One agreement rate over both would stay high for a judge that always
says true, because most steps are right.

Every action handling_plan_v6 wrote on the held-out set copies the SOP step's text word for word (62 of 62),
so the judge's "true" there is easy; the reworded actions come from v2 to v5.

## Results

All plans and all judge calls used claude-sonnet-5-5. The data is synthetic. Each row is one results file in
`eval/results/` (last column: the time in its name).

- P, R, F1: precision and recall of the actions against the expected SOP steps.
- S1: alarms that no SOP covers; right only if the plan has no steps. They have no expected steps, so P and R
  do not count them.
- flags: alarms whose flags (E1, W1, W2, R1, WF1, S1) were all right; only the sets that have expected flags.
- $ / plan and s / plan: per plan Claude wrote (S1 and duplicate alarms make no Claude call).

| prompt version: what changed | set | judge | P | R | F1 | S1 | flags | $ / plan | s / plan | file |
|---|---|---|---|---|---|---|---|---|---|---|
| v1: no SOPs in the prompt (baseline) | golden_v1 | v1 | 0.00 (0/125) | 0.00 | 0.00 | 0/3 | | | 9.3 | 061723 |
| v2: all 40 SOPs in the system prompt | golden_v1 | v1 | 0.78 (59/76) | 0.92 | 0.84 | 0/3 | | 0.024* | 6.8 | 062641 |
| the same v2 plans, scored again | golden_v1 | v2 | 0.82 (62/76) | 0.97 | 0.89 | 0/3 | | | | 182511 |
| the same v2 plans, scored again | golden_v1 | v3 | 0.83 (63/76) | 0.98 | 0.90 | 0/3 | | | | 183226 |
| v2, a new run | golden_v1 | v3 | 0.83 (64/77) | 1.00 | 0.91 | 0/3 | | | 7.5 | 203401 |
| v3: "the system adds escalation, don't write it as an action" | golden_v1 | v3 | 0.91 (62/68) | 0.97 | 0.94 | 0/3 | | 0.023* | 6.6 | 202752 |
| v4: retrieval, only the exact-match SOPs in the prompt; S1 decided by code | golden_v1 | v3 | 0.91 (63/69) | 0.98 | 0.95 | 3/3 | | 0.011* | 5.8 | 014058 |
| v5: flags by code, plus two prompt sentences about them | golden_v2 | v3 | 0.94 (62/66) | 0.97 | 0.95 | 3/3 | 21/21 | 0.010* | 5.1 | 025715 |
| v6: the flags go into the prompt as system notes | golden_v2 | v3 | 1.00 (64/64) | 1.00 | 1.00 | 3/3 | 21/21 | 0.0098* | 4.3 | 031035 |
| v6, 3 runs | heldout_v1 | v3 | 1.00 (62/62) each | 1.00 | 1.00 | 2/2 | 20/20 | 0.0106-0.0108 | 4.9-5.1 | 171401, 214832, 215241 |
| v7: agent with 3 lookup tools | heldout_v1 | v3 | 1.00 (62/62) | 1.00 | 1.00 | 2/2 | 20/20 | 0.0199 | 8.1 | 214233 |

\* From the `llm_calls` audit log (tokens at $2 / $10 per million input / output); these runs were made before
`run_eval.py` recorded tokens. The held-out rows are from the results files.

What the rows show:
- Putting the SOPs in the prompt took P from 0 to 0.78. After that, most wrong steps were things code should own
  (escalation, flags); moving them to code and telling the model about them took P to 1.00.
- The judge prompt alone moved P from 0.78 to 0.83 on the same plans, so rows are only compared with the same judge.
- Retrieval (v4) kept P and R, cut the cost per plan by about half (0.023 to 0.011), and fixed S1 (0/3 to 3/3),
  which P and R do not show.
- v6's 1.00 on golden_v2 is on the set its prompt was written from. The held-out set, never used to change a
  prompt, also gave 1.00 in 3 runs out of 3.
- The agent (v7) matched v6 on the held-out set but cost 88% more and took 59% longer per plan, so every alarm
  uses v6.

Judge check (`judge_check.py`, judge_v3, file `judge_check_20261119_210129`): the judge said false on 20 of 20
known-wrong steps (added operation 7/7, different operation 7/7, part of the step left out 6/6). Whether it
wrongly rejects right, reworded steps is not measured yet.

## Not used, and why

- Fine-tuning. A fine-tuned small model pays off only when all four hold: the task is fixed, the output is one of
  a few labels, there are thousands of examples labelled by people, and the answer is needed in milliseconds.
  The one model call here writes a plan of several free-text steps with their sources, not one of a few labels;
  there are 40 labelled alarms (golden and held-out), made up for this project, not thousands; and an engineer
  reviews every plan, so a few seconds is fine. The parts that really are classification (which SOP applies,
  the flags) are rules in code and need no model at all.
- LangGraph. The agent loop has two paths (call a tool, or finish), and the engineer's review is the plan's status
  in the database. Nothing has to pause in the middle of a run or resume after a crash, so a hand-written loop
  is enough.
- Claude Agent SDK. No part of the system needs an agent that reads and writes files or runs commands.

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

Run the held-out set:

```
docker compose run --rm -e GOLDEN_SET_VERSION=heldout_v1 eval
```

Run one alarm several times, to see how much its result changes from run to run (each run makes a new plan):

```
docker compose run --rm eval python run_eval.py --only AL-063 --repeat 5
```

Check the judge against the labelled steps (needs the API key, not the other services):

```
docker compose run --rm --no-deps eval python judge_check.py
```
