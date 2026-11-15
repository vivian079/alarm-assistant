# Judge prompt versions

Each version is a folder with `system.txt` and `user.txt`.
`JUDGE_PROMPT_VERSION` in `.env` says which folder `judge.py` uses.
A version is never edited after it has been used; a change is a new folder.

`user.txt` is filled with `{action}` (one action's text) and `{sop_steps}` (from `compare.sop_context`).

| version | what changed |
|---|---|
| judge_v1 | judges the action against the cited SOP text only |
| judge_v2 | gets the whole cited SOP with the cited steps marked `<- CITED`, so it can read "all of the above" |
| judge_v3 | extra words that add no operation (a reason, a reminder, a value from the alarm) are fine |

Note on judge_v1: it was used with the `compare.py` of commit 3baf91b, which passed only the cited steps.
Today's `compare.py` passes the whole SOP, so judge_v1 is kept as a record of what ran, not to be run again.
