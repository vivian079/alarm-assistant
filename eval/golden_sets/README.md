# Golden set versions

`GOLDEN_SET_VERSION` in `.env` says which file `run_eval.py` uses.
A version is never edited after it has been used: fixing an expected answer means a new file (`golden_v2.json`).

| version | what it has |
|---|---|
| golden_v1 | 20 alarms: normal 6, step_specific 5, repeat 3, not_covered 3, in_spec 2, same_event 1; 64 expected steps |
