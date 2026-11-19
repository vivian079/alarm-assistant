"""api/rules.py against the flags written by hand in the golden and held-out sets."""

import json
from pathlib import Path

import pytest

import retrieval
import rules

ROOT = Path(__file__).resolve().parent.parent


def flag_codes(alarm: dict, code: dict) -> set[str]:
    """The flag codes main.create_plan saves for this alarm, checked in the same order."""
    duplicate = rules.duplicate_flag(alarm)
    if duplicate is not None:  # E1 stops everything else
        return {duplicate["code"]}
    flags = rules.check_flags(alarm, code)
    if not retrieval.find_sops(alarm["alarm_code"], code["equipment_type"], alarm["process_step"]):
        flags = [rules.not_covered_flag(alarm)] + flags  # S1
    return {f["code"] for f in flags}


@pytest.mark.parametrize("set_name", ["golden_v2", "heldout_v1"])
def test_flags_match_the_expected_flags(set_name, fake_db, alarms, codes):
    golden = json.loads((ROOT / "eval" / "golden_sets" / f"{set_name}.json").read_text())
    wrong = {}
    for g in golden:
        alarm = alarms[g["alarm_id"]]
        got = flag_codes(alarm, codes[alarm["alarm_code"]])
        if got != set(g["expected_flags"]):
            wrong[g["alarm_id"]] = {"expected": sorted(g["expected_flags"]), "got": sorted(got)}
    assert wrong == {}


def test_repeat_within_7_days(fake_db, alarms, codes):
    # AL-017: ETCH-03 chamber B, A-1023 on 11-03; the same tool reported A-1023 on 11-01 (EV-001).
    flags = rules.check_flags(alarms["AL-017"], codes["A-1023"])
    r1 = [f for f in flags if f["code"] == "R1"]
    assert r1 and r1[0]["related"] == ["EV-001"]


def test_other_chamber_within_30_minutes_is_the_same_event_not_a_repeat(fake_db, alarms, codes):
    # AL-041: ETCH-03 chamber B at 10:30; AL-040 was chamber A at 10:05, same tool and code.
    codes_found = {f["code"] for f in rules.check_flags(alarms["AL-041"], codes["A-1023"])}
    assert "W1" in codes_found and "R1" not in codes_found


def test_the_later_of_two_identical_entries_is_the_duplicate(fake_db, alarms):
    # AL-050 and AL-051 are the same alarm entered twice: only the later one is rejected.
    assert rules.duplicate_flag(alarms["AL-050"]) is None
    assert rules.duplicate_flag(alarms["AL-051"])["related"] == ["AL-050"]
