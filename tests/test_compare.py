"""eval/compare.py: the code checks that run before the judge, and the edge cases of the scores."""

import compare

SOPS = {"SOP-012": {"title": "Chamber pressure high during main etch",
                    "steps": [{"step": 1, "text": "Compare the reading with the backup pressure sensor."},
                              {"step": 2, "text": "Check the gas inlet flow."}]}}
EXPECTED = [{"sop_id": "SOP-012", "step": 1}, {"sop_id": "SOP-012", "step": 2}]


def test_the_judge_is_only_called_after_the_code_checks_pass():
    called = []

    def judge(action_text, sop_text):
        called.append(action_text)
        return True

    actions = [
        {"step": "no source", "sop_refs": []},
        {"step": "a source that is not expected", "sop_refs": [{"sop_id": "SOP-013", "step": 1}]},
        {"step": "an expected source", "sop_refs": [{"sop_id": "SOP-012", "step": 1}]},
    ]
    result = compare.score_actions(actions, EXPECTED, SOPS, judge)
    assert called == ["an expected source"]
    assert result == {"generated": 3, "correct": 1, "covered": 1, "expected": 2}


def test_judge_false_means_the_action_is_not_right():
    actions = [{"step": "Check the gas inlet flow and lower the setpoint.", "sop_refs": [{"sop_id": "SOP-012", "step": 2}]}]
    result = compare.score_actions(actions, EXPECTED, SOPS, lambda action_text, sop_text: False)
    assert result["correct"] == 0 and result["covered"] == 0


def test_the_judge_sees_the_whole_sop_with_the_cited_step_marked():
    text = compare.sop_context(SOPS, [("SOP-012", 2)])
    assert "step 1: Compare the reading with the backup pressure sensor.\n" in text
    assert "step 2: Check the gas inlet flow.   <- CITED" in text


def test_zero_over_zero_is_none_not_zero():
    assert compare.precision_recall_f1(0, 0, 0, 0) == (None, None, None)
    assert compare.precision_recall_f1(0, 5, 0, 4) == (0.0, 0.0, 0.0)
    assert compare.precision_recall_f1(4, 4, 4, 4) == (1.0, 1.0, 1.0)


def test_an_alarm_no_sop_covers_is_right_only_with_no_steps():
    assert compare.score_not_covered([]) is True
    assert compare.score_not_covered([{"step": "Notify maintenance.", "sop_refs": []}]) is False


def test_flags_report_what_is_missing_and_what_is_extra():
    result = compare.score_flags(["R1", "WF1"], ["WF1", "W2"])
    assert result["right"] is False
    assert result["missing"] == ["R1"] and result["extra"] == ["W2"]
