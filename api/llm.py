import os

import anthropic

from prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

# Reads ANTHROPIC_API_KEY from the environment.
client = anthropic.Anthropic()


def generate_plan(alarm: dict) -> str:
    """Fill the alarm record into the prompt, call Claude, return the plan text.

    Synchronous: this waits until Claude returns the whole answer.
    """
    user_prompt = USER_PROMPT_TEMPLATE.format(**alarm)
    response = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    # The answer can contain a thinking block before the text block,
    # so keep only the text blocks.
    return "".join(block.text for block in response.content if block.type == "text")
