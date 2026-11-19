"""eval/judge_check.py: the two label groups are counted apart, and a failed judge call is not a catch."""

import json
from pathlib import Path

import judge
import judge_check

ROOT = Path(__file__).resolve().parent.parent


def row(item_id, label, judgement, mutation=None, failed=False):
    return {"id": item_id, "label": label, "judgement": judgement, "mutation": mutation, "judge_failed": failed}


def test_groups_are_counted_apart_and_failed_calls_are_left_out(monkeypatch):
    monkeypatch.setattr(judge, "LOG", [])
    rows = [
        row("M-01", False, False, "added_operation"),                 # wrong step, judge caught it
        row("M-02", False, True, "added_operation"),                  # wrong step, judge missed it
        row("M-03", False, False, "left_out_part", failed=True),      # the call failed: not counted as caught
        row("R-01", True, True),                                      # right step, judge agreed
        row("R-02", None, True),                                      # not labelled yet
    ]
    totals = judge_check.summarize(rows)
    assert totals["label_false"] == {"agree": 1, "total": 2,
                                     "by_mutation": {"added_operation": {"agree": 1, "total": 2}}}
    assert totals["label_true"] == {"agree": 1, "total": 1, "by_mutation": {}}
    assert totals["disagreements"] == ["M-02"]
    assert totals["judge_failed"] == ["M-03"]
    assert totals["not_labelled"] == ["R-02"]


def test_the_wrong_steps_cite_a_real_step_and_change_its_text():
    items = json.loads((ROOT / "eval" / "judge_sets" / "judge_check_v1.json").read_text())
    sops = {s["sop_id"]: s for s in json.loads((ROOT / "data" / "sops.json").read_text())}
    assert len({i["id"] for i in items}) == len(items)
    for item in items:
        if item["kind"] != "mutated":
            continue
        assert item["label"] is False and item["mutation"] in {"added_operation", "different_content", "left_out_part"}
        (ref,) = item["sop_refs"]
        sop_text = next(st["text"] for st in sops[ref["sop_id"]]["steps"] if st["step"] == ref["step"])
        assert item["action"] != sop_text and item["source"]["sop_text"] == sop_text
