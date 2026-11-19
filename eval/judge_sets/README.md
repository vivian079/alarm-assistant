# Judge check sets

Each file is a list of steps with a person's label, used by `judge_check.py` to measure the judge
(`judge.py`) in both directions: does it say false on wrong steps, and true on right ones.

Item fields:
- `id`: `M-..` for a changed (mutated) step, `R-..` for a real action from a run
- `kind`: `mutated` or `real`
- `mutation`: how a mutated step was made wrong: `added_operation`, `different_content`, `left_out_part`
- `sop_refs`, `action`: what the judge gets (the action text and the SOP steps it cites)
- `label`: the person's answer, by the same rule the judge has: true if the action does the operation of the
  cited step; false if it adds an operation, does a different one, or leaves part of the step out.
  A reason, a reminder or a value from the alarm added to the text does not make it false. `null`: not labelled yet.
- `judgement`, `judge_reason`, `judge_prompt_version`, `judge_model`: for a real action, the verdict from the run
  it came from (`source.run`); `judge_check.py` reuses it when the judge version and model are the same.
  For a mutated step these are empty and the step is judged when the check runs.
- `source`: the run and alarm a real action came from, or the SOP text a mutated step was changed from.

A set is not edited after it has been used; a change is a new file.

`judge_check_v1.json`:
- M-01 to M-20: one SOP step each, the text changed by hand; the cited step is right, so the code check passes.
- R-01 to R-20 (not labelled yet, so not counted): picked at random (Python `random.Random(9).sample`) from the 92 real actions that reached the judge
  in judge_v3 runs, were judged true, and whose text is not a word-for-word copy of the cited step.
