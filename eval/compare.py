"""Scoring. run_eval.py calls these five functions.

What the inputs look like (plain dicts and lists, read from JSON):
  summary        plan["alarm_summary"], written by the LLM:
                 {"tool_id": "ETCH-03", "chamber": "B", "alarm_code": "A-1023", "sensor_value": 58.0,
                  "spec_lower": 20.0, "spec_upper": 50.0, "unit": "mTorr", "wafer_count": 25,
                  "recent_alarm": "...", "recent_fix": "..."}
  record         this alarm's item in data/alarm_records.json
  code           this alarm code's item in data/alarm_codes.json
  actions        plan["actions"], written by the LLM:
                 [{"step": "Compare the reading with ...", "sop_refs": [{"sop_id": "SOP-012", "step": 1}]}, ...]
  expected_refs  expected_sop_refs from golden_set.json: [{"sop_id": "SOP-012", "step": 1}, ...]
  sops           {"SOP-012": {..., "steps": [{"step": 1, "text": "Compare the reading with ..."}, ...]}, ...}
  judge          judge(action_text, cited_sop_text) -> bool   (one Claude call each time)
"""


# recent_alarm and recent_fix are not here: they are free text, so == can't check them.
RECORD_FIELDS = ["tool_id", "chamber", "alarm_code", "sensor_value", "wafer_count"]
CODE_FIELDS = ["spec_lower", "spec_upper", "unit"]


def score_summary(summary: dict, record: dict, code: dict) -> dict:
    """Return {"correct": int, "total": int, "wrong": list[str]}.
    wrong = names of the fields that don't match.
    """
    wrong = []
    for field in RECORD_FIELDS:
        if summary[field] != record[field]:
            wrong.append(field)
    for field in CODE_FIELDS:
        if summary[field] != code[field]:
            wrong.append(field)
    total = len(RECORD_FIELDS) + len(CODE_FIELDS)
    return {"correct": total - len(wrong), "total": total, "wrong": wrong}


def score_actions(actions: list[dict], expected_refs: list[dict], sops: dict, judge) -> dict:
    """Return {"generated": int, "correct": int, "covered": int, "expected": int}.
    generated  how many actions the plan has
    correct    how many of those actions count as right
    covered    how many expected steps are covered by the right actions
    expected   how many expected steps there are

    An action is right only if all three hold:
    1. it has at least one SOP reference (no source -> not right)
    2. every reference is one of the expected steps (a made-up or wrong SOP step -> not right)
    3. the judge says its text does what those SOP steps say (a real source with other text -> not right)
    Checks 1 and 2 are code; the judge (an LLM call) only runs when they pass.
    """
    expected = {(r["sop_id"], r["step"]) for r in expected_refs}
    covered = set()
    correct = 0

    for action in actions:
        refs = [(r["sop_id"], r["step"]) for r in action["sop_refs"]]
        if not refs:
            continue
        if any(ref not in expected for ref in refs):
            continue
        # One judge call per action. The judge gets every step of each cited SOP, with the cited steps marked,
        # because a step can refer to other steps ("If all of the above are normal, ...").
        cited_text = sop_context(sops, refs)
        if not judge(action["step"], cited_text):
            continue
        correct += 1
        covered.update(refs)

    return {"generated": len(actions), "correct": correct, "covered": len(covered), "expected": len(expected)}


def sop_context(sops: dict, refs: list[tuple[str, int]]) -> str:
    """Full text of each cited SOP, cited steps marked with <- CITED."""
    blocks = []
    for sop_id in dict.fromkeys(sop_id for sop_id, _ in refs):  # each SOP once, in order
        sop = sops[sop_id]
        lines = [f"{sop_id}: {sop['title']}"]
        for st in sop["steps"]:
            mark = "   <- CITED" if (sop_id, st["step"]) in refs else ""
            lines.append(f"  step {st['step']}: {st['text']}{mark}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def score_not_covered(actions: list[dict]) -> bool:
    """For an alarm whose expected_sop_refs is []. Return True if the plan handled it right.
    Rule S1: no SOP covers it, so the plan must have no steps at all.
    """
    return len(actions) == 0


def precision_recall_f1(correct: int, generated: int, covered: int, expected: int):
    """Totals -> (precision, recall, f1).
    0/0 has no value, so it returns None instead of 0:
    "generated nothing" and "generated only wrong steps" must not look the same.
    """
    precision = correct / generated if generated else None
    recall = covered / expected if expected else None
    if precision is None or recall is None:
        f1 = None
    elif precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return precision, recall, f1


def score_flags(expected: list[str], actual: list[str]) -> dict:
    """Flags as codes ("R1", "W2", ...). expected from the golden set, actual from the plan the API saved.
    Return {"expected", "actual", "right", "missing", "extra"}: right only if the two sets are the same;
    missing = expected but not raised, extra = raised but not expected.
    """
    exp, act = set(expected), set(actual)
    return {
        "expected": sorted(exp), "actual": sorted(act), "right": exp == act,
        "missing": sorted(exp - act), "extra": sorted(act - exp),
    }
