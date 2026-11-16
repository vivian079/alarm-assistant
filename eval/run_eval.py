"""Run the golden set through the API and score the handling plans.

Usage (from the repo root, with the API running):
    docker compose run --rm eval
Re-score the plans of an earlier run without generating new ones (to test a change to the scoring or the judge):
    docker compose run --rm eval python run_eval.py --rescore results/run_<time>.json
Run only some alarms, several times each (to see how much one alarm's result changes from run to run):
    docker compose run --rm eval python run_eval.py --only AL-063 --repeat 5

For each alarm in golden_sets/<GOLDEN_SET_VERSION>.json:
1. POST /alarms/{alarm_id}/plan  -> the same endpoint the page uses, so we test what the engineer gets
2. alarm_summary vs the data tables         -> compare.score_summary
3. actions vs expected_sop_refs             -> compare.score_actions (needs the judge)
   or, when expected_sop_refs is []         -> compare.score_not_covered
Then print the totals and save everything to results/run_<time>_<prompt version>_<judge version>.json.

Every results file records the four things that change the scores:
  prompt_version, model   from the API's response (the API made the plan, so it knows what it used)
  judge_prompt_version, judge_model, golden_set_version   from this script's own .env (it uses them itself)
"""

import argparse
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
GOLDEN_SET_VERSION = os.environ["GOLDEN_SET_VERSION"]


def load(path: Path):
    return json.loads(path.read_text())


def run_one(g: dict, records: dict, codes: dict, sops: dict) -> dict:
    """Generate a new plan through the API, then score it."""
    alarm_id = g["alarm_id"]
    start = time.time()
    resp = requests.post(f"{API_URL}/alarms/{alarm_id}/plan", timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    saved = resp.json()  # the handling_plans row the API stored

    row = {
        "alarm_id": alarm_id,
        "case": g["case"],
        "plan_id": saved["plan_id"],
        "request_id": saved["request_id"],          # finds this plan's calls in llm_calls
        "prompt_version": saved["prompt_version"],  # what the API used, not what .env says now
        "model": saved["model"],
        "status": saved["status"],
        "attempts": saved["attempts"],
        "latency_s": round(time.time() - start, 1),
        "expected_steps": len(g["expected_sop_refs"]),
    }
    if saved["status"] == "generation_failed":
        row["error"] = saved["error"]
        return row
    row["plan"] = saved["plan_json"]
    return score(row, g, records, codes, sops)


def score(row: dict, g: dict, records: dict, codes: dict, sops: dict) -> dict:
    """Score a row that already has its plan."""
    plan = row["plan"]
    record = records[g["alarm_id"]]
    row["summary"] = compare.score_summary(plan["alarm_summary"], record, codes[record["alarm_code"]])
    if g["expected_sop_refs"]:
        row["actions"] = compare.score_actions(plan["actions"], g["expected_sop_refs"], sops, judge.judge_action)
    else:
        row["not_covered_correct"] = compare.score_not_covered(plan["actions"])
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
        "judge_input_tokens": sum(e.get("input_tokens") or 0 for e in judge.LOG),
        "judge_output_tokens": sum(e.get("output_tokens") or 0 for e in judge.LOG),
        "avg_latency_s": round(sum(r["latency_s"] for r in rows) / len(rows), 1),
        "by_case": case_scores,
    }


def fmt(x) -> str:
    """precision_recall_f1 returns None for 0/0."""
    return "n/a" if x is None else f"{x:.2f}"


def run_versions(rows: list[dict]) -> dict:
    """The four things that change the scores, for the whole run."""
    # From the API's responses. Should be one value; more than one means the API was restarted
    # with another version in the middle of the run, and the scores mix two prompts.
    # "unknown": a re-score of a results file written before versions were recorded.
    prompts = sorted({r.get("prompt_version") or "unknown" for r in rows})
    models = sorted({r.get("model") or "unknown" for r in rows})
    return {
        "prompt_version": "+".join(prompts),
        "model": "+".join(models),
        "judge_prompt_version": judge.PROMPT_VERSION,
        "judge_model": judge.MODEL,
        "golden_set_version": GOLDEN_SET_VERSION,
    }


def print_report(rows: list[dict], t: dict, v: dict) -> None:
    print()
    print("  ".join(f"{k} {val}" for k, val in v.items()))
    if "+" in v["prompt_version"]:
        print("WARNING: plans in this run came from more than one prompt version")
    print()
    repeated = any(r.get("run", 1) > 1 for r in rows)
    for r in rows:
        run = f" #{r['run']}" if repeated else ""
        line = f"{r['alarm_id']}{run}  {r['case']:<14}"
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
    print(f"Judge tokens: input {t['judge_input_tokens']}  output {t['judge_output_tokens']}")
    print()
    for name, c in t["by_case"].items():
        print(f"  {name:<14} P {fmt(c['precision'])}  R {fmt(c['recall'])}  F1 {fmt(c['f1'])}")


def print_repeats(rows: list[dict]) -> None:
    """For an alarm run more than once: its covered count in each run, side by side."""
    by_alarm: dict[str, list[str]] = {}
    for r in rows:
        if r["status"] == "generation_failed":
            by_alarm.setdefault(r["alarm_id"], []).append("failed")
        elif "actions" in r:
            by_alarm.setdefault(r["alarm_id"], []).append(f"{r['actions']['covered']}/{r['actions']['expected']}")
        else:
            by_alarm.setdefault(r["alarm_id"], []).append("no steps" if r["not_covered_correct"] else "has steps")
    print()
    for alarm_id, results in by_alarm.items():
        if len(results) > 1:
            print(f"{alarm_id} covered in each run: {', '.join(results)}")


def save(rows: list[dict], totals: dict, versions: dict, rescored_from: str | None = None, tag: str = "") -> Path:
    out_dir = EVAL_DIR / "results"
    out_dir.mkdir(exist_ok=True)
    # The versions are in the file name too, so the list of files shows what each run was.
    # tag marks a partial run (e.g. "_AL-063x5"), so it is not mistaken for a full golden set run.
    name = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{versions['prompt_version']}_{versions['judge_prompt_version']}{tag}"
    path = out_dir / f"{name}.json"
    path.write_text(json.dumps(
        {"run_at": datetime.now().isoformat(timespec="seconds"), "versions": versions,
         "rescored_from": rescored_from, "totals": totals, "rows": rows, "judge_log": judge.LOG},
        indent=2, ensure_ascii=False,
    ))
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rescore", help="results file of an earlier run: score its plans again, no new plans")
    parser.add_argument("--only", nargs="+", metavar="ALARM_ID", help="run only these golden alarms")
    parser.add_argument("--repeat", type=int, default=1, help="generate a plan for each alarm this many times")
    args = parser.parse_args()
    if args.rescore and args.repeat > 1:
        parser.error("--repeat makes new plans; --rescore reuses old ones. Use one of them.")

    golden = load(EVAL_DIR / "golden_sets" / f"{GOLDEN_SET_VERSION}.json")
    golden_by_id = {g["alarm_id"]: g for g in golden}
    if args.only:
        unknown = [a for a in args.only if a not in golden_by_id]
        if unknown:
            parser.error(f"not in {GOLDEN_SET_VERSION}: {', '.join(unknown)}")
    records = {r["alarm_id"]: r for r in load(DATA_DIR / "alarm_records.json")}
    codes = {c["alarm_code"]: c for c in load(DATA_DIR / "alarm_codes.json")}
    sops = {s["sop_id"]: s for s in load(DATA_DIR / "sops.json")}

    # What to do: a list of (golden alarm, old row to re-score or None to make a new plan, run number).
    if args.rescore:
        # Go through the old file's rows, so a partial or repeated run is re-scored as it was.
        old_rows = load(Path(args.rescore))["rows"]
        todo = [(golden_by_id[r["alarm_id"]], r, r.get("run", 1)) for r in old_rows
                if not args.only or r["alarm_id"] in args.only]
    else:
        selected = [g for g in golden if not args.only or g["alarm_id"] in args.only]
        todo = [(g, None, k) for g in selected for k in range(1, args.repeat + 1)]

    rows = []
    for i, (g, old, run) in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {g['alarm_id']} ({g['case']})" + (f" run {run}" if args.repeat > 1 else ""), flush=True)
        if old is not None:
            # The plan is reused, so its prompt_version and model come along with it.
            row = {k: old[k] for k in ("alarm_id", "case", "plan_id", "request_id", "prompt_version", "model",
                                       "status", "attempts", "latency_s", "expected_steps", "error", "plan")
                   if k in old}
            row["run"] = run
            rows.append(row if row["status"] == "generation_failed" else score(row, g, records, codes, sops))
        else:
            row = run_one(g, records, codes, sops)
            row["run"] = run
            rows.append(row)

    totals = summarize(rows)
    versions = run_versions(rows)
    print_report(rows, totals, versions)
    print_repeats(rows)
    tag = ""
    if args.only:
        tag = "_" + "-".join(args.only) + (f"x{args.repeat}" if args.repeat > 1 else "")
    elif args.rescore:
        ids = [r["alarm_id"] for r in rows]
        if len(set(ids)) < len(golden) or len(ids) != len(set(ids)):
            tag = "_partial"  # re-score of a run that was not one full pass over the golden set
    print(f"\nSaved to {save(rows, totals, versions, args.rescore, tag)}")


if __name__ == "__main__":
    main()
