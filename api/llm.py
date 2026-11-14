import os

import anthropic
from pydantic import ValidationError

from prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from schemas import HandlingPlan

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
MAX_TOKENS = 8000
MAX_TOKENS_AFTER_CUTOFF = 16000
MAX_RETRIES = 2  # first try + 2 retries = at most 3 API calls

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


def generate_plan(alarm: dict) -> tuple[HandlingPlan, int]:
    """Fill the alarm into the prompt, call Claude with the schema, return (plan, attempts).

    Synchronous: each call waits until Claude returns the whole answer.
    Raises PlanGenerationError if every try fails.
    """
    messages = [{"role": "user", "content": USER_PROMPT_TEMPLATE.format(**alarm)}]
    max_tokens = MAX_TOKENS
    error = ""

    for attempt in range(1, MAX_RETRIES + 2):
        response = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=messages,
            output_config={"format": OUTPUT_FORMAT},
        )

        if response.stop_reason == "refusal":
            # The same input would be refused again, so don't retry.
            raise PlanGenerationError("Claude refused to answer", attempt)

        if response.stop_reason == "max_tokens":
            # Cut off: the JSON is incomplete. Retry the same request with more room.
            # Don't ask for a shorter answer: a shorter plan can drop SOP steps.
            error = f"output was cut off at max_tokens={max_tokens}"
            max_tokens = MAX_TOKENS_AFTER_CUTOFF
            continue

        # The answer can contain a thinking block before the text block,
        # so keep only the text blocks.
        text = "".join(block.text for block in response.content if block.type == "text")
        try:
            return HandlingPlan.model_validate_json(text), attempt
        except ValidationError as e:
            # Retry with the error message: show Claude its output and what was wrong with it.
            error = str(e)
            messages = messages + [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": f"Your output did not pass validation:\n{error}\n"
                    "Return the complete handling plan again with this fixed.",
                },
            ]

    raise PlanGenerationError(error, MAX_RETRIES + 1)
