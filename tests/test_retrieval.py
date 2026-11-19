"""Exact-match retrieval (api/retrieval.find_sops) against the SOPs each alarm is expected to cite."""

import json
from pathlib import Path

import pytest

import retrieval

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("set_name", ["golden_v2", "heldout_v1"])
def test_exact_match_finds_exactly_the_expected_sops(set_name, alarms, codes):
    golden = json.loads((ROOT / "eval" / "golden_sets" / f"{set_name}.json").read_text())
    wrong = {}
    for g in golden:
        if g["case"] == "duplicate":  # rejected by E1 before retrieval
            continue
        alarm = alarms[g["alarm_id"]]
        code = codes[alarm["alarm_code"]]
        found = {s["sop_id"] for s in retrieval.find_sops(alarm["alarm_code"], code["equipment_type"],
                                                          alarm["process_step"])}
        expected = {r["sop_id"] for r in g["expected_sop_refs"]}  # empty for the S1 cases
        if found != expected:
            wrong[g["alarm_id"]] = {"expected": sorted(expected), "found": sorted(found)}
    assert wrong == {}


def test_same_code_different_process_step_gets_a_different_sop():
    # A-1023 (chamber pressure too high): SOP-012 in MAIN-ETCH, SOP-013 in OVER-ETCH, none in CHAMBER-CLEAN.
    ids = lambda step: [s["sop_id"] for s in retrieval.find_sops("A-1023", "etch", step)]
    assert ids("MAIN-ETCH") == ["SOP-012"]
    assert ids("OVER-ETCH") == ["SOP-013"]
    assert ids("CHAMBER-CLEAN") == []
