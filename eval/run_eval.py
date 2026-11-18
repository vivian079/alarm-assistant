"""Run the golden set through the API and score the handling plans.

Usage (from the repo root, with the API running):
    docker compose run --rm eval
Re-score the plans of an earlier run without generating new ones (to test a change to the scoring or the judge):
    docker compose run --rm eval python run_eval.py --rescore results/run_<time>.json
Run only some alarms, several times each (to see how much one alarm's result changes from run to run):
    docker compose run --rm eval python run_eval.py --only AL-063 --repeat 5
Run the held-out set instead of the golden set (GOLDEN_SET_VERSION picks the file):
    docker compose run --rm -e GOLDEN_SET_VERSION=heldout_v1 eval

For each alarm in golden_sets/<GOLDEN_SET_VERSION>.json:
1. POST /alarms/{alarm_id}/plan  -> the same endpoint the page uses, so we test what the engineer gets
2. retrieved_sop_ids vs the SOP IDs in expected_sop_refs -> check_retrieval (prompt versions with retrieval only)
3. flags vs expected_flags                  -> compare.score_flags (golden sets that have expected_flags)
   a "duplicate" alarm (rule E1) is only checked for being rejected; it has no plan to score
4. alarm_summary vs the data tables         -> compare.score_summary
5. actions vs expected_sop_refs             -> compare.score_actions (needs the judge)
   or, when expected_sop_refs is []         -> compare.score_not_covered
Then print the totals and save everything to results/run_<time>_<prompt version>_<judge version>_<set>.json.

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
REQUEST_TIMEOUT = 5 * 180  # same as the page: one request can make up to 5 Claude calls (agent versions)
# $ per million tokens (input, output), to estimate the cost of the plans. Check the current price before quoting it.
# Haiku 5.5's price is for prompts up to 100K tokens (ours are about 2K).
PRICES = {"claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}
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
        # None: this prompt version sends the whole library, nothing was retrieved
        "retrieved_sop_ids": saved.get("retrieved_sop_ids"),
        # None: plans saved before flags existed
        "actual_flags": [f["code"] for f in saved["flags"]] if saved.get("flags") is not None else None,
        # What making the plan cost, over all its calls. None: no Claude call (S1, E1) or saved before these existed.
        "input_tokens": saved.get("input_tokens"),
        "output_tokens": saved.get("output_tokens"),
        "tool_calls": saved.get("tool_calls"),  # agent versions: [{"name", "input"}]; None for others
    }
    if saved.get("error"):
        row["error"] = saved["error"]
    if saved.get("plan_json") is not None:
        row["plan"] = saved["plan_json"]
    return finish_row(row, g, records, codes, sops)


def finish_row(row: dict, g: dict, records: dict, codes: dict, sops: dict) -> dict:
    """Every check that applies to a row that came back from the API (or from an old results file)."""
    check_retrieval(row, g)
    check_flags(row, g)
    if g["case"] == "duplicate":
        # Rule E1: right only if the API refused to make a plan. Nothing else to score.
        row["duplicate_rejected"] = row["status"] == "rejected_duplicate"
        return row
    if "plan" not in row:
        return row  # generation_failed (or wrongly rejected as a duplicate): no plan to score
    return score(row, g, records, codes, sops)


def check_flags(row: dict, g: dict) -> None:
    if "expected_flags" not in g or row.get("actual_flags") is None:
        return
    row["flags"] = compare.score_flags(g["expected_flags"], row["actual_flags"])


def check_retrieval(row: dict, g: dict) -> None:
    """Did retrieval put the right SOPs into the prompt? Checked apart from the plan:
    if the right SOP is not in the prompt, Claude can't cite it, however good the prompt is.
    expected = the SOP IDs in expected_sop_refs ([] for an alarm no SOP covers).
    """
    if row.get("retrieved_sop_ids") is None:
        return
    expected = {r["sop_id"] for r in g["expected_sop_refs"]}
    retrieved = set(row["retrieved_sop_ids"])
    row["retrieval"] = {
        "expected": sorted(expected),
        "retrieved": sorted(retrieved),
        "found_all": expected <= retrieved,     # no expected SOP missing
        "extra": len(retrieved - expected),     # SOPs in the prompt that the answer doesn't use
    }


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
    dup_ok = dup_total = 0
    by_case: dict[str, dict] = {}
    for r in rows:
        if "duplicate_rejected" in r:
            dup_total += 1
            dup_ok += int(r["duplicate_rejected"])
            continue
        c = by_case.setdefault(r["case"], {"correct": 0, "generated": 0, "covered": 0, "expected": 0})
        if "summary" not in r:
            # No plan came back: no steps were generated, and every expected step was missed.
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

    # Flags: per alarm (all right or not) and per flag code.
    flag_rows = [r["flags"] for r in rows if "flags" in r]
    by_flag: dict[str, dict] = {}
    for f in flag_rows:
        for code in f["expected"]:
            d = by_flag.setdefault(code, {"expected": 0, "raised": 0, "missing": 0, "extra": 0})
            d["expected"] += 1
            d["raised" if code in f["actual"] else "missing"] += 1
        for code in f["extra"]:
            by_flag.setdefault(code, {"expected": 0, "raised": 0, "missing": 0, "extra": 0})["extra"] += 1

    # Cost: only rows where Claude was called (S1 and E1 rows made no call). Failed ones count: their calls are billed.
    llm_rows = [r for r in rows if r["status"] not in ("sop_not_covered", "rejected_duplicate")]
    in_tok = sum(r.get("input_tokens") or 0 for r in llm_rows)
    out_tok = sum(r.get("output_tokens") or 0 for r in llm_rows)
    models = {r.get("model") for r in llm_rows}
    price = PRICES.get(models.pop()) if len(models) == 1 else None
    cost = round((in_tok * price[0] + out_tok * price[1]) / 1e6, 4) if price and llm_rows else None
    tool_counts: dict[str, int] = {}
    for r in llm_rows:
        for t in r.get("tool_calls") or []:
            tool_counts[t["name"]] = tool_counts.get(t["name"], 0) + 1
    n = len(llm_rows) or 1

    return {
        "precision": p, "recall": rcl, "f1": f1,
        "correct": cor, "generated": gen, "covered": cov, "expected": exp,
        "not_covered_correct": nc_ok, "not_covered_total": nc_total,
        "summary_fields_correct": sum_ok, "summary_fields_total": sum_total,
        "failed": sum(r["status"] == "generation_failed" for r in rows),
        "retrieval_checked": sum("retrieval" in r for r in rows),
        "retrieval_found_all": sum(r["retrieval"]["found_all"] for r in rows if "retrieval" in r),
        "retrieval_extra": sum(r["retrieval"]["extra"] for r in rows if "retrieval" in r),
        "flags_checked": len(flag_rows),
        "flags_all_right": sum(f["right"] for f in flag_rows),
        "by_flag": dict(sorted(by_flag.items())),
        "duplicates_rejected": dup_ok, "duplicates_total": dup_total,
        "judge_calls": len(judge.LOG),
        "judge_input_tokens": sum(e.get("input_tokens") or 0 for e in judge.LOG),
        "judge_output_tokens": sum(e.get("output_tokens") or 0 for e in judge.LOG),
        "avg_latency_s": round(sum(r["latency_s"] for r in rows) / len(rows), 1),
        "llm_plans": len(llm_rows),
        "api_calls": sum(r.get("attempts") or 0 for r in llm_rows),
        "avg_latency_s_llm": round(sum(r["latency_s"] for r in llm_rows) / n, 1),
        "input_tokens": in_tok, "output_tokens": out_tok,
        "avg_input_tokens": round(in_tok / n), "avg_output_tokens": round(out_tok / n),
        "cost_usd": cost, "avg_cost_usd": round(cost / n, 4) if cost is not None else None,
        "tool_calls": sum(tool_counts.values()), "tool_calls_by_name": dict(sorted(tool_counts.items())),
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
    # sop_not_covered and rejected_duplicate rows are left out: code decided them (rules S1, E1), no prompt or model made them.
    made_by_llm = [r for r in rows if r["status"] not in ("sop_not_covered", "rejected_duplicate")]
    prompts = sorted({r.get("prompt_version") or "unknown" for r in made_by_llm})
    models = sorted({r.get("model") or "unknown" for r in made_by_llm})
    return {
        "prompt_version": "+".join(prompts) or "none",
        "model": "+".join(models) or "none",
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
        flag_note = ""
        if "flags" in r:
            f = r["flags"]
            flag_note = f"  flags {','.join(f['actual']) or '-'}" + ("" if f["right"] else
                f" WRONG (missing {','.join(f['missing']) or '-'}, extra {','.join(f['extra']) or '-'})")
        if "duplicate_rejected" in r:
            print(line + "  duplicate: " + ("rejected, right" if r["duplicate_rejected"] else "NOT rejected, wrong") + flag_note)
            continue
        if "summary" not in r:
            print(line + ("  REJECTED AS DUPLICATE" if r["status"] == "rejected_duplicate" else "  GENERATION FAILED") + flag_note)
            continue
        s = r["summary"]
        line += f"  summary {s['correct']}/{s['total']}"
        if "actions" in r:
            a = r["actions"]
            line += f"  actions right {a['correct']}/{a['generated']}  covered {a['covered']}/{a['expected']}"
        else:
            line += "  no steps: " + ("right" if r["not_covered_correct"] else f"wrong ({len(r['plan']['actions'])} steps)")
            if r["status"] == "sop_not_covered":
                line += " (S1 by code, no Claude call)"
        if "retrieval" in r:
            line += f"  SOPs in prompt {','.join(r['retrieval']['retrieved']) or '-'}"
        if r.get("tool_calls"):
            line += "  tools " + ",".join(f"{t['name']}({','.join(str(v) for v in t['input'].values())})"
                                          for t in r["tool_calls"])
        print(line + flag_note)

    print()
    print(f"Precision {fmt(t['precision'])}   Recall {fmt(t['recall'])}   F1 {fmt(t['f1'])}")
    print(f"  (right {t['correct']} / generated {t['generated']}, covered {t['covered']} / expected {t['expected']})")
    print(f"SOP not covered: {t['not_covered_correct']}/{t['not_covered_total']} handled right")
    if t["retrieval_checked"]:
        print(f"Retrieval: expected SOPs all in the prompt for {t['retrieval_found_all']}/{t['retrieval_checked']} alarms, "
              f"extra SOPs {t['retrieval_extra']}")
    if t["flags_checked"]:
        print(f"Flags: all right for {t['flags_all_right']}/{t['flags_checked']} alarms   " + "   ".join(
            f"{code} {d['raised']}/{d['expected']} extra {d['extra']}" for code, d in t["by_flag"].items()))
    if t["duplicates_total"]:
        print(f"Duplicates (E1) rejected: {t['duplicates_rejected']}/{t['duplicates_total']}")
    print(f"alarm_summary fields: {t['summary_fields_correct']}/{t['summary_fields_total']} right")
    print(f"Failed generations: {t['failed']}   Judge calls: {t['judge_calls']}   Avg latency: {t['avg_latency_s']} s")
    print(f"Judge tokens: input {t['judge_input_tokens']}  output {t['judge_output_tokens']}")
    cost = f"${t['avg_cost_usd']:.4f} per plan, ${t['cost_usd']:.2f} in all" if t["cost_usd"] is not None else "n/a"
    print(f"Plans made by Claude: {t['llm_plans']}   API calls: {t['api_calls']}   "
          f"Avg latency {t['avg_latency_s_llm']} s   Avg tokens: input {t['avg_input_tokens']}  output {t['avg_output_tokens']}")
    print(f"Est. cost (plans, judge not included): {cost}")
    if t["tool_calls"]:
        print(f"Tool calls: {t['tool_calls']}  " + "  ".join(f"{k} {v}" for k, v in t["tool_calls_by_name"].items()))
    print()
    for name, c in t["by_case"].items():
        print(f"  {name:<14} P {fmt(c['precision'])}  R {fmt(c['recall'])}  F1 {fmt(c['f1'])}")


def print_repeats(rows: list[dict]) -> None:
    """For an alarm run more than once: its covered count in each run, side by side."""
    by_alarm: dict[str, list[str]] = {}
    for r in rows:
        if "duplicate_rejected" in r:
            by_alarm.setdefault(r["alarm_id"], []).append("rejected" if r["duplicate_rejected"] else "not rejected")
        elif "summary" not in r:
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
    name = (f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{versions['prompt_version']}_"
            f"{versions['judge_prompt_version']}_{versions['golden_set_version']}{tag}")
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
                                       "status", "attempts", "latency_s", "expected_steps", "error", "plan",
                                       "retrieved_sop_ids", "actual_flags", "input_tokens", "output_tokens",
                                       "tool_calls")
                   if k in old}
            row["run"] = run
            rows.append(finish_row(row, g, records, codes, sops))
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
