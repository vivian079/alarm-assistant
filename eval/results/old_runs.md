# Runs from before versions were recorded

These four files only have a date and time in their name. What each one ran with
(worked out from the commit history: each run was committed right after it ran):

| file | prompt | judge | golden set | model | note |
|---|---|---|---|---|---|
| run_20261115_061723.json | handling_plan_v1 | judge_v1 | golden_v1 | claude-sonnet-5-5 | baseline, no SOPs in the prompt: P 0 R 0 |
| run_20261115_062641.json | handling_plan_v2 | judge_v1 | golden_v1 | claude-sonnet-5-5 | all 40 SOPs in the prompt: P 0.78 R 0.92 |
| run_20261115_182511.json | handling_plan_v2 | judge_v2 | golden_v1 | claude-sonnet-5-5 | re-score of 062641's plans, judge sees the whole SOP: P 0.82 R 0.97 |
| run_20261115_183226.json | handling_plan_v2 | judge_v3 | golden_v1 | claude-sonnet-5-5 | re-score of 062641's plans, reminders are fine: P 0.83 R 0.98 |

New runs record their versions inside the file and in the file name.
