"""LLM-as-Judge: does an action's text do what the SOP step(s) it cites say?

A separate Claude call from the one that writes the handling plan.
Code checks that the cited SOP step exists and is expected; the judge only reads the two texts.
The prompt is in judge_prompts/<JUDGE_PROMPT_VERSION>/ (system.txt, user.txt).
user.txt is filled with .format(action=..., sop_steps=...):
  action     = the text of one action in the handling plan
  sop_steps  = every step of each SOP this action cites; the cited steps end with "<- CITED" (compare.sop_context)
The output format (reason, judgement) is set by the Judge class below, not in the prompt.
"""

import os
import time
from pathlib import Path

import anthropic
from pydantic import BaseModel, ValidationError

MODEL = os.environ.get("CLAUDE_JUDGE_MODEL", "claude-sonnet-5-5")

# Which judge prompt to use. Set in .env.
PROMPT_VERSION = os.environ["JUDGE_PROMPT_VERSION"]
PROMPT_DIR = Path(__file__).parent / "judge_prompts" / PROMPT_VERSION
if not PROMPT_DIR.is_dir():
    raise RuntimeError(f"JUDGE_PROMPT_VERSION={PROMPT_VERSION}: there is no folder {PROMPT_DIR}")
# Each file ends with one newline, which is not part of the prompt.
JUDGE_SYSTEM_PROMPT = (PROMPT_DIR / "system.txt").read_text().removesuffix("\n")
JUDGE_USER_TEMPLATE = (PROMPT_DIR / "user.txt").read_text().removesuffix("\n")

# Reads ANTHROPIC_API_KEY from the environment.
client = anthropic.Anthropic()


class Judge(BaseModel):
    reason: str      # first, so the model writes its reasoning before the verdict
    judgement: bool  # True: the action does what the SOP step says


OUTPUT_FORMAT = {
    "type": "json_schema",
    "schema": anthropic.transform_schema(Judge.model_json_schema()),
}

# Every judge call goes here, so run_eval.py can save the reasons and token counts with the results
# (to check the judge against a person's labels later, and to count what the eval costs).
LOG: list[dict] = []


def judge_action(action_text: str, sop_steps_text: str) -> bool:
    """One Claude call. Returns the judgement. If the call fails, counts as False (not a match)."""
    entry = {"action": action_text, "sop_steps": sop_steps_text}
    start = time.monotonic()
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
        entry.update(input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens)
        text = "".join(block.text for block in response.content if block.type == "text")
        verdict = Judge.model_validate_json(text)
    except (anthropic.APIError, ValidationError) as e:
        # Precision comes first: when the judge can't answer, don't count the action as right.
        entry.update(reason=f"judge failed: {e}", judgement=False,
                     latency_ms=int((time.monotonic() - start) * 1000))
        LOG.append(entry)
        return False

    entry.update(reason=verdict.reason, judgement=verdict.judgement,
                 latency_ms=int((time.monotonic() - start) * 1000))
    LOG.append(entry)
    return verdict.judgement
