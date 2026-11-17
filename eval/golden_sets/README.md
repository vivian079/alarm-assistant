# Golden set versions

`GOLDEN_SET_VERSION` in `.env` says which file `run_eval.py` uses.
A version is never edited after it has been used: fixing an expected answer means a new file (`golden_v2.json`).

| version | what it has |
|---|---|
| golden_v1 | 20 alarms: normal 6, step_specific 5, repeat 3, not_covered 3, in_spec 2, same_event 1; 64 expected steps |
| golden_v2 | golden_v1's 20 alarms with the same expected steps, plus `expected_flags` (E1, W1, W2, R1, S1, WF1 from the design doc rules) and `flag_reason`; plus AL-051 (case `duplicate`: same as AL-050, expected flag E1 and no plan) |
