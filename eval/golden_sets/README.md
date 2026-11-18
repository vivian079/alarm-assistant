# Golden set versions

`GOLDEN_SET_VERSION` in `.env` says which file `run_eval.py` uses.
A version is never edited after it has been used: fixing an expected answer means a new file (`golden_v2.json`).

| version | what it has |
|---|---|
| golden_v1 | 20 alarms: normal 6, step_specific 5, repeat 3, not_covered 3, in_spec 2, same_event 1; 64 expected steps |
| golden_v2 | golden_v1's 20 alarms with the same expected steps, plus `expected_flags` (E1, W1, W2, R1, S1, WF1 from the design doc rules) and `flag_reason`; plus AL-051 (case `duplicate`: same as AL-050, expected flag E1 and no plan) |
| heldout_v1 | held-out set: 20 new alarms (AL-076 to AL-095): normal 4, step_specific 3, repeat 2, not_covered 2, in_spec 2, same_event 1, duplicate 1, and history_context 5 (the tool recently reported other alarm codes, the case where an agent may look things up); 62 expected steps; same fields as golden_v2 |

Held-out rule: a held-out set only reports numbers. Never change a prompt because of what it got wrong on the
held-out set; once you do, it is one more golden set and a new held-out set is needed.
The expected answers follow the same rules as the golden set: the steps come only from the SOP that matches this
alarm's code and process step, and history (repeats, other alarm codes, repairs) never changes the steps.
