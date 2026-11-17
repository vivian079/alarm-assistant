import os
import time
from pathlib import Path

import anthropic
from pydantic import ValidationError

import db
import retrieval
from schemas import HandlingPlan

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
MAX_TOKENS = 8000
MAX_TOKENS_AFTER_CUTOFF = 16000
MAX_RETRIES = 2  # first try + 2 retries = at most 3 API calls

# Which prompt version to use. Set in .env; read once when the API starts,
# so after changing .env the API has to be restarted.
PROMPT_VERSION = os.environ["PROMPT_VERSION"]
PROMPT_DIR = Path(__file__).parent / "prompts"


def load_prompt(version: str) -> tuple[str, str]:
    """prompts/<version>/system.txt and user.txt. Each file ends with one newline, which is not part of the prompt."""
    folder = PROMPT_DIR / version
    if not folder.is_dir():
        raise RuntimeError(f"PROMPT_VERSION={version}: there is no folder {folder}")
    system = (folder / "system.txt").read_text().removesuffix("\n")
    user = (folder / "user.txt").read_text().removesuffix("\n")
    return system, user


SYSTEM_TEMPLATE, USER_PROMPT_TEMPLATE = load_prompt(PROMPT_VERSION)

# Where the SOPs go depends on the prompt version:
#   {sop_library} in system.txt     -> all SOPs, the same for every alarm (handling_plan_v2, v3)
#   {retrieved_sops} in user.txt    -> retrieval (handling_plan_v4 on): only the SOPs exact match finds
#                                      for this alarm; if it finds none, rule S1 and no Claude call
#   neither                         -> no SOPs (handling_plan_v1)
USES_RETRIEVAL = "{retrieved_sops}" in USER_PROMPT_TEMPLATE
if USES_RETRIEVAL and "{sop_library}" in SYSTEM_TEMPLATE:
    raise RuntimeError(f"PROMPT_VERSION={PROMPT_VERSION} has both {{sop_library}} and {{retrieved_sops}}")

# replace, not format: SOP text could contain { }.
SYSTEM = SYSTEM_TEMPLATE.replace("{sop_library}", retrieval.format_sops(retrieval.SOPS))

# Reads ANTHROPIC_API_KEY from the environment.
client = anthropic.Anthropic()

# HandlingPlan -> JSON schema -> the format Structured Outputs accepts.
# transform_schema adjusts the schema to what the API supports (e.g. adds additionalProperties: false).
OUTPUT_FORMAT = {
    "type": "json_schema",
    "schema": anthropic.transform_schema(HandlingPlan.model_json_schema()),
}


class PlanGenerationError(Exception):
    """Every try failed. attempts = how many API calls were made."""

    def __init__(self, message: str, attempts: int):
        super().__init__(message)
        self.attempts = attempts


def generate_plan(alarm: dict, request_id: str) -> tuple[HandlingPlan, int]:
    """Fill the alarm into the prompt, call Claude with the schema, return (plan, attempts).

    Synchronous: each call waits until Claude returns the whole answer.
    Every API call is written to the llm_calls table (one row per call, failed ones too),
    with request_id so the rows can be found from the handling plan.
    Raises PlanGenerationError if every try fails.
    """
    messages = [{"role": "user", "content": USER_PROMPT_TEMPLATE.format(**alarm)}]
    max_tokens = MAX_TOKENS
    error = ""

    for attempt in range(1, MAX_RETRIES + 2):
        call = {
            "request_id": request_id,
            "alarm_id": alarm["alarm_id"],
            "attempt": attempt,
            "prompt_version": PROMPT_VERSION,
            "model": MODEL,
            "system_prompt": SYSTEM,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        start = time.monotonic()
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=SYSTEM,
                messages=messages,
                output_config={"format": OUTPUT_FORMAT},
            )
        except anthropic.APIError as e:
            # No answer came back (network, rate limit, server error after the SDK's own retries).
            # Still one call, so it gets a row.
            call["latency_ms"] = int((time.monotonic() - start) * 1000)
            call["error"] = f"API error: {e}"
            db.log_llm_call(call)
            raise PlanGenerationError(call["error"], attempt)

        call["latency_ms"] = int((time.monotonic() - start) * 1000)
        call["stop_reason"] = response.stop_reason
        call["input_tokens"] = response.usage.input_tokens
        call["output_tokens"] = response.usage.output_tokens
        # The answer can contain a thinking block before the text block,
        # so keep only the text blocks.
        text = "".join(block.text for block in response.content if block.type == "text")
        call["response_text"] = text

        if response.stop_reason == "refusal":
            # The same input would be refused again, so don't retry.
            call["error"] = "Claude refused to answer"
            db.log_llm_call(call)
            raise PlanGenerationError(call["error"], attempt)

        if response.stop_reason == "max_tokens":
            # Cut off: the JSON is incomplete. Retry the same request with more room.
            # Don't ask for a shorter answer: a shorter plan can drop SOP steps.
            error = f"output was cut off at max_tokens={max_tokens}"
            call["error"] = error
            db.log_llm_call(call)
            max_tokens = MAX_TOKENS_AFTER_CUTOFF
            continue

        try:
            plan = HandlingPlan.model_validate_json(text)
        except ValidationError as e:
            # Retry with the error message: show Claude its output and what was wrong with it.
            error = str(e)
            call["error"] = error
            db.log_llm_call(call)
            messages = messages + [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": f"Your output did not pass validation:\n{error}\n"
                    "Return the complete handling plan again with this fixed.",
                },
            ]
            continue

        db.log_llm_call(call)  # passed: error stays empty
        return plan, attempt

    raise PlanGenerationError(error, MAX_RETRIES + 1)
