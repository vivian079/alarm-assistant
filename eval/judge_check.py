"""Check the judge against a person's labels, and report each label group on its own.

P on every run counts an action as right only when the judge (judge.py) says its text does what the cited
SOP step says. So we need to know how often the judge is wrong, in both directions:
  label false  steps known to be wrong. Each cites a real expected SOP step, so the code check would pass
               and the judge is called; only the text was changed: an operation added, a different operation,
               or part of the step left out. Did the judge say false?
  label true   real actions from earlier runs whose text was reworded (not copied from the SOP), labelled by
               a person. Did the judge say true?

The two groups are reported separately. One agreement rate over both would hide a judge that misses wrong
steps: most steps in a plan are right, so a judge that always says true still gets a high rate.

Usage (from the repo root; needs only ANTHROPIC_API_KEY and JUDGE_PROMPT_VERSION from .env, not the API):
    docker compose run --rm --no-deps eval python judge_check.py
    docker compose run --rm --no-deps eval python judge_check.py --set judge_check_v1 --rejudge

An item that already has a verdict from the same judge prompt version and model (the real actions keep the
verdict from the run they came from) is not judged again, unless --rejudge. Every other item is judged now.
Items not labelled yet ("label": null) are not judged and not counted.
Saves results/judge_check_<time>_<judge version>_<set>.json.
"""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import compare
import judge
from run_eval import PRICES

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
EVAL_DIR = Path(__file__).parent


def judge_items(items: list[dict], sops: dict, rejudge: bool) -> list[dict]:
    rows = []
    for item in items:
        row = {**item, "judged_now": False, "judge_failed": False}
        same_judge = (item["judgement"] is not None and item["judge_prompt_version"] == judge.PROMPT_VERSION
                      and item["judge_model"] == judge.MODEL)
        if item["label"] is None or (same_judge and not rejudge):
            rows.append(row)
            continue
        print(f"judging {item['id']}", flush=True)
        refs = [(r["sop_id"], r["step"]) for r in item["sop_refs"]]
        verdict = judge.judge_action(item["action"], compare.sop_context(sops, refs))
        reason = judge.LOG[-1]["reason"]
        row.update(judgement=verdict, judge_reason=reason, judge_prompt_version=judge.PROMPT_VERSION,
                   judge_model=judge.MODEL, judged_now=True,
                   # judge_action returns False when the call fails. Here that would look like the judge
                   # catching a wrong step, so a failed call is kept out of the counts.
                   judge_failed=reason.startswith("judge failed:"))
        rows.append(row)
    return rows


def summarize(rows: list[dict]) -> dict:
    counted = [r for r in rows if r["label"] is not None and not r["judge_failed"]]
    groups = {}
    for label in (False, True):
        group = [r for r in counted if r["label"] is label]
        # For the wrong steps, also count by how the step was made wrong ("real": a real action a person labelled false).
        by_mutation = {}
        if label is False:
            for m in sorted({r["mutation"] or "real" for r in group}):
                g = [r for r in group if (r["mutation"] or "real") == m]
                by_mutation[m] = {"agree": sum(r["judgement"] is label for r in g), "total": len(g)}
        groups[str(label).lower()] = {"agree": sum(r["judgement"] is label for r in group), "total": len(group),
                                      "by_mutation": by_mutation}
    agree = sum(r["judgement"] is r["label"] for r in counted)
    in_tok = sum(e.get("input_tokens") or 0 for e in judge.LOG)
    out_tok = sum(e.get("output_tokens") or 0 for e in judge.LOG)
    price = PRICES.get(judge.MODEL)
    return {
        "label_false": groups["false"], "label_true": groups["true"],
        "agreement": {"agree": agree, "total": len(counted)},
        "disagreements": [r["id"] for r in counted if r["judgement"] is not r["label"]],
        "judge_failed": [r["id"] for r in rows if r["judge_failed"]],
        "not_labelled": [r["id"] for r in rows if r["label"] is None],
        "judge_calls": len(judge.LOG), "judge_input_tokens": in_tok, "judge_output_tokens": out_tok,
        "est_cost": round((in_tok * price[0] + out_tok * price[1]) / 1e6, 4) if price else None,
    }


def ratio(c: dict) -> str:
    return f"{c['agree']}/{c['total']}" + (f" = {c['agree'] / c['total']:.2f}" if c["total"] else "")


def print_report(rows: list[dict], t: dict, set_name: str) -> None:
    by_id = {r["id"]: r for r in rows}
    print(f"\nJudge check: {set_name}, judge {judge.PROMPT_VERSION} ({judge.MODEL})")
    print(f"Label false (wrong steps): judge said false {ratio(t['label_false'])}")
    for m, c in t["label_false"]["by_mutation"].items():
        print(f"  {m:<18} {c['agree']}/{c['total']}")
    print(f"Label true (right steps):  judge said true  {ratio(t['label_true'])}")
    print(f"Agreement over both: {ratio(t['agreement'])}  (depends on how many of each the set has; "
          f"read the two lines above)")
    print("Disagreements:" + ("" if t["disagreements"] else " none"))
    for i in t["disagreements"]:
        r = by_id[i]
        print(f"  {i} label {r['label']}, judge {r['judgement']}: {r['action']}")
        print(f"      judge's reason: {r['judge_reason']}")
    if t["judge_failed"]:
        print(f"Judge call failed (not counted): {', '.join(t['judge_failed'])}")
    if t["not_labelled"]:
        print(f"Not labelled yet (not counted): {', '.join(t['not_labelled'])}")
    print(f"New judge calls: {t['judge_calls']}   tokens: input {t['judge_input_tokens']}  "
          f"output {t['judge_output_tokens']}   est. cost: ${t['est_cost']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", default="judge_check_v1", help="file in judge_sets/, without .json")
    parser.add_argument("--rejudge", action="store_true", help="judge every labelled item now")
    args = parser.parse_args()

    items = json.loads((EVAL_DIR / "judge_sets" / f"{args.set}.json").read_text())
    sops = {s["sop_id"]: s for s in json.loads((DATA_DIR / "sops.json").read_text())}

    rows = judge_items(items, sops, args.rejudge)
    totals = summarize(rows)
    print_report(rows, totals, args.set)

    out = EVAL_DIR / "results" / (f"judge_check_{datetime.now().strftime('%Y%m%d_%H%M%S')}_"
                                  f"{judge.PROMPT_VERSION}_{args.set}.json")
    out.write_text(json.dumps(
        {"run_at": datetime.now().isoformat(timespec="seconds"), "set": args.set,
         "judge_prompt_version": judge.PROMPT_VERSION, "judge_model": judge.MODEL,
         "totals": totals, "items": rows, "judge_log": judge.LOG},
        indent=2, ensure_ascii=False,
    ))
    print(f"\nSaved to {out}")


if __name__ == "__main__":
    main()
