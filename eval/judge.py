"""LLM-as-Judge: does an action's text do what the SOP step(s) it cites say?

A separate Claude call from the one that writes the handling plan.
Code checks that the cited SOP step exists and is expected; the judge only reads the two texts.
The prompt is in judge_prompt.py.
"""

import os

import anthropic
from pydantic import BaseModel, ValidationError

from judge_prompt import JUDGE_SYSTEM_PROMPT, JUDGE_USER_TEMPLATE

MODEL = os.environ.get("CLAUDE_JUDGE_MODEL", "claude-sonnet-5-5")

# Reads ANTHROPIC_API_KEY from the environment.
client = anthropic.Anthropic()


class Judge(BaseModel):
    reason: str      # first, so the model writes its reasoning before the verdict
    judgement: bool  # True: the action does what the SOP step says


OUTPUT_FORMAT = {
    "type": "json_schema",
    "schema": anthropic.transform_schema(Judge.model_json_schema()),
}

# Every judge call goes here, so run_eval.py can save the reasons with the results
# (to check the judge against a person's labels later).
LOG: list[dict] = []


def judge_action(action_text: str, sop_steps_text: str) -> bool:
    """One Claude call. Returns the judgement. If the call fails, counts as False (not a match)."""
    entry = {"action": action_text, "sop_steps": sop_steps_text}
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1000,
            system=JUDGE_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": JUDGE_USER_TEMPLATE.format(action=action_text, sop_steps=sop_steps_text),
            }],
            output_config={"format": OUTPUT_FORMAT},
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        verdict = Judge.model_validate_json(text)
    except (anthropic.APIError, ValidationError) as e:
        # Precision comes first: when the judge can't answer, don't count the action as right.
        entry.update(reason=f"judge failed: {e}", judgement=False)
        LOG.append(entry)
        return False

    entry.update(reason=verdict.reason, judgement=verdict.judgement)
    LOG.append(entry)
    return verdict.judgement
