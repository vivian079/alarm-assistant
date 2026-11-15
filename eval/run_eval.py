"""Run the golden set through the API and score the handling plans.

Usage (from the repo root, with the API running):
    docker compose run --rm eval

For each alarm in golden_set.json:
1. POST /alarms/{alarm_id}/plan  -> the same endpoint the page uses, so we test what the engineer gets
2. alarm_summary vs the data tables         -> compare.score_summary
3. actions vs expected_sop_refs             -> compare.score_actions (needs the judge)
   or, when expected_sop_refs is []         -> compare.score_not_covered
Then print the totals and save everything to results/run_<time>.json.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path

import requests

import compare
import judge

API_URL = os.environ.get("API_URL", "http://localhost:8000")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
EVAL_DIR = Path(__file__).parent
REQUEST_TIMEOUT = 3 * 180  # same as the page: one request can make up to 3 Claude calls


def load(path: Path):
    return json.loads(path.read_text())


def run_one(g: dict, records: dict, codes: dict, sops: dict) -> dict:
    alarm_id = g["alarm_id"]
    start = time.time()
    resp = requests.post(f"{API_URL}/alarms/{alarm_id}/plan", timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    saved = resp.json()  # the handling_plans row the API stored

    row = {
        "alarm_id": alarm_id,
        "case": g["case"],
        "plan_id": saved["plan_id"],
        "status": saved["status"],
        "attempts": saved["attempts"],
        "latency_s": round(time.time() - start, 1),
        "expected_steps": len(g["expected_sop_refs"]),
    }
    if saved["status"] == "generation_failed":
        row["error"] = saved["error"]
        return row

    plan = saved["plan_json"]
    record = records[alarm_id]
    row["summary"] = compare.score_summary(plan["alarm_summary"], record, codes[record["alarm_code"]])
    if g["expected_sop_refs"]:
        row["actions"] = compare.score_actions(plan["actions"], g["expected_sop_refs"], sops, judge.judge_action)
    else:
        row["not_covered_correct"] = compare.score_not_covered(plan["actions"])
    row["plan"] = plan
    return row


def summarize(rows: list[dict]) -> dict:
    gen = cor = cov = exp = 0
    sum_ok = sum_total = 0
    nc_ok = nc_total = 0
    by_case: dict[str, dict] = {}
    for r in rows:
        c = by_case.setdefault(r["case"], {"correct": 0, "generated": 0, "covered": 0, "expected": 0})
        if r["status"] == "generation_failed":
            # Nothing came back: no steps were generated, and every expected step was missed.
            exp += r["expected_steps"]
            c["expected"] += r["expected_steps"]
            if r["expected_steps"] == 0:
                nc_total += 1
            continue
        sum_ok += r["summary"]["correct"]
        sum_total += r["summary"]["total"]
        if "actions" in r:
            a = r["actions"]
            gen, cor, cov, exp = gen + a["generated"], cor + a["correct"], cov + a["covered"], exp + a["expected"]
            for k in c:
                c[k] += a[k]
        else:
            nc_total += 1
            nc_ok += int(r["not_covered_correct"])

    p, rcl, f1 = compare.precision_recall_f1(cor, gen, cov, exp)
    case_scores = {}
    for name, c in by_case.items():
        if name == "not_covered":
            continue
        cp, cr, cf = compare.precision_recall_f1(c["correct"], c["generated"], c["covered"], c["expected"])
        case_scores[name] = {**c, "precision": cp, "recall": cr, "f1": cf}

    return {
        "precision": p, "recall": rcl, "f1": f1,
        "correct": cor, "generated": gen, "covered": cov, "expected": exp,
        "not_covered_correct": nc_ok, "not_covered_total": nc_total,
        "summary_fields_correct": sum_ok, "summary_fields_total": sum_total,
        "failed": sum(r["status"] == "generation_failed" for r in rows),
        "judge_calls": len(judge.LOG),
        "avg_latency_s": round(sum(r["latency_s"] for r in rows) / len(rows), 1),
        "by_case": case_scores,
    }


def fmt(x) -> str:
    """precision_recall_f1 returns None for 0/0."""
    return "n/a" if x is None else f"{x:.2f}"


def print_report(rows: list[dict], t: dict) -> None:
    print()
    for r in rows:
        line = f"{r['alarm_id']}  {r['case']:<14}"
        if r["status"] == "generation_failed":
            print(line + "  GENERATION FAILED")
            continue
        s = r["summary"]
        line += f"  summary {s['correct']}/{s['total']}"
        if "actions" in r:
            a = r["actions"]
            line += f"  actions right {a['correct']}/{a['generated']}  covered {a['covered']}/{a['expected']}"
        else:
            line += "  no steps: " + ("right" if r["not_covered_correct"] else f"wrong ({len(r['plan']['actions'])} steps)")
        print(line)

    print()
    print(f"Precision {fmt(t['precision'])}   Recall {fmt(t['recall'])}   F1 {fmt(t['f1'])}")
    print(f"  (right {t['correct']} / generated {t['generated']}, covered {t['covered']} / expected {t['expected']})")
    print(f"SOP not covered: {t['not_covered_correct']}/{t['not_covered_total']} handled right")
    print(f"alarm_summary fields: {t['summary_fields_correct']}/{t['summary_fields_total']} right")
    print(f"Failed generations: {t['failed']}   Judge calls: {t['judge_calls']}   Avg latency: {t['avg_latency_s']} s")
    print()
    for name, c in t["by_case"].items():
        print(f"  {name:<14} P {fmt(c['precision'])}  R {fmt(c['recall'])}  F1 {fmt(c['f1'])}")


def save(rows: list[dict], totals: dict) -> Path:
    out_dir = EVAL_DIR / "results"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(
        {"run_at": datetime.now().isoformat(timespec="seconds"), "judge_model": judge.MODEL,
         "totals": totals, "rows": rows, "judge_log": judge.LOG},
        indent=2, ensure_ascii=False,
    ))
    return path


def main() -> None:
    golden = load(EVAL_DIR / "golden_set.json")
    records = {r["alarm_id"]: r for r in load(DATA_DIR / "alarm_records.json")}
    codes = {c["alarm_code"]: c for c in load(DATA_DIR / "alarm_codes.json")}
    sops = {s["sop_id"]: s for s in load(DATA_DIR / "sops.json")}

    rows = []
    for i, g in enumerate(golden, 1):
        print(f"[{i}/{len(golden)}] {g['alarm_id']} ({g['case']})", flush=True)
        rows.append(run_one(g, records, codes, sops))

    totals = summarize(rows)
    print_report(rows, totals)
    print(f"\nSaved to {save(rows, totals)}")


if __name__ == "__main__":
    main()
