import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
from pydantic import ValidationError

import db
import retrieval
import tools
from schemas import HandlingPlan

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
MAX_TOKENS = 8000
MAX_TOKENS_AFTER_CUTOFF = 16000
MAX_RETRIES = 2  # first try + 2 retries = at most 3 API calls
# Agent versions (with tools.json): the most API calls one plan may make, tool turns and retries together.
# If Claude still has no valid plan after this many calls, the plan is saved as generation_failed.
MAX_TURNS = 5

# Which prompt version to use. Set in .env; read once when the API starts,
# so after changing .env the API has to be restarted.
PROMPT_VERSION = os.environ["PROMPT_VERSION"]
PROMPT_DIR = Path(__file__).parent / "prompts"


def load_prompt(version: str) -> tuple[str, str, list[dict] | None]:
    """prompts/<version>/system.txt, user.txt, and tools.json if the version has one.
    Each text file ends with one newline, which is not part of the prompt.
    The tool descriptions are part of the prompt (Claude reads them to decide what to call),
    so they are versioned with it.
    """
    folder = PROMPT_DIR / version
    if not folder.is_dir():
        raise RuntimeError(f"PROMPT_VERSION={version}: there is no folder {folder}")
    system = (folder / "system.txt").read_text().removesuffix("\n")
    user = (folder / "user.txt").read_text().removesuffix("\n")
    tools_file = folder / "tools.json"
    tool_defs = json.loads(tools_file.read_text()) if tools_file.exists() else None
    return system, user, tool_defs


SYSTEM_TEMPLATE, USER_PROMPT_TEMPLATE, TOOLS = load_prompt(PROMPT_VERSION)

# Where the SOPs go depends on the prompt version:
#   {sop_library} in system.txt     -> all SOPs, the same for every alarm (handling_plan_v2, v3)
#   {retrieved_sops} in user.txt    -> retrieval (handling_plan_v4 on): only the SOPs exact match finds
#                                      for this alarm; if it finds none, rule S1 and no Claude call
#   neither                         -> no SOPs (handling_plan_v1)
USES_RETRIEVAL = "{retrieved_sops}" in USER_PROMPT_TEMPLATE
if USES_RETRIEVAL and "{sop_library}" in SYSTEM_TEMPLATE:
    raise RuntimeError(f"PROMPT_VERSION={PROMPT_VERSION} has both {{sop_library}} and {{retrieved_sops}}")
if TOOLS is not None:
    unknown = [t["name"] for t in TOOLS if t["name"] not in tools.FUNCTIONS]
    if unknown:
        raise RuntimeError(f"PROMPT_VERSION={PROMPT_VERSION}: tools.json has tools with no function: {unknown}")

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


@dataclass
class Usage:
    """What one plan generation used, over all its API calls. Saved on the handling plan."""
    calls: int = 0           # API calls made (= attempts)
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: list[dict] = field(default_factory=list)  # [{"name": ..., "input": {...}}], in order


class PlanGenerationError(Exception):
    """Every try failed. usage = what was spent on the way."""

    def __init__(self, message: str, usage: Usage):
        super().__init__(message)
        self.usage = usage


def content_as_dicts(response) -> list[dict]:
    """The response's content blocks as plain dicts: for the log, and to send back as the assistant turn."""
    out = []
    for block in response.content:
        if block.type == "text":
            out.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            out.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
    return out


def new_call(alarm: dict, request_id: str, attempt: int, messages: list, max_tokens: int) -> dict:
    """One llm_calls row, before the call is made."""
    return {
        "request_id": request_id,
        "alarm_id": alarm["alarm_id"],
        "attempt": attempt,
        "prompt_version": PROMPT_VERSION,
        "model": MODEL,
        "system_prompt": SYSTEM,
        "messages": messages,
        "max_tokens": max_tokens,
    }


def record_response(call: dict, response, start: float, usage: Usage) -> str:
    """Fill the row from the response and add its tokens to usage. Returns the text blocks joined."""
    call["latency_ms"] = int((time.monotonic() - start) * 1000)
    call["stop_reason"] = response.stop_reason
    call["input_tokens"] = response.usage.input_tokens
    call["output_tokens"] = response.usage.output_tokens
    usage.input_tokens += response.usage.input_tokens
    usage.output_tokens += response.usage.output_tokens
    # The whole content, so a turn that only asked for a tool still shows which tool and what input.
    call["response_content"] = content_as_dicts(response)
    # The answer can contain a thinking block before the text block, so keep only the text blocks.
    text = "".join(block.text for block in response.content if block.type == "text")
    call["response_text"] = text
    return text


def generate_plan(alarm: dict, request_id: str) -> tuple[HandlingPlan, Usage]:
    """Fill the alarm into the prompt, call Claude with the schema, return (plan, usage).

    Synchronous: each call waits until Claude returns the whole answer.
    Every API call is written to the llm_calls table (one row per call, failed ones too),
    with request_id so the rows can be found from the handling plan.
    Raises PlanGenerationError if every try fails.
    Prompt versions with tools.json go to generate_plan_with_tools instead.
    """
    if TOOLS is not None:
        return generate_plan_with_tools(alarm, request_id)

    usage = Usage()
    messages = [{"role": "user", "content": USER_PROMPT_TEMPLATE.format(**alarm)}]
    max_tokens = MAX_TOKENS
    error = ""

    for attempt in range(1, MAX_RETRIES + 2):
        usage.calls = attempt
        call = new_call(alarm, request_id, attempt, messages, max_tokens)
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
            raise PlanGenerationError(call["error"], usage)

        text = record_response(call, response, start, usage)

        if response.stop_reason == "refusal":
            # The same input would be refused again, so don't retry.
            call["error"] = "Claude refused to answer"
            db.log_llm_call(call)
            raise PlanGenerationError(call["error"], usage)

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
        return plan, usage

    raise PlanGenerationError(error, usage)


def generate_plan_with_tools(alarm: dict, request_id: str) -> tuple[HandlingPlan, Usage]:
    """The agent loop: Claude may call the tools in tools.json before it writes the plan.

    Each turn is one API call (one llm_calls row):
      stop_reason "tool_use"  -> run each tool it asked for, add its turn and the results to messages, call again
      stop_reason "end_turn"  -> the text is the plan: validate it, retry with the error if it fails
      "max_tokens"            -> retry with more room; "refusal" -> stop
    At most MAX_TURNS calls in all; after that the plan is saved as generation_failed.
    Every call sends the whole conversation so far: Claude only sees what is in messages.
    """
    usage = Usage()
    messages = [{"role": "user", "content": USER_PROMPT_TEMPLATE.format(**alarm)}]
    max_tokens = MAX_TOKENS
    retries = 0
    error = ""

    while usage.calls < MAX_TURNS:
        usage.calls += 1
        call = new_call(alarm, request_id, usage.calls, messages, max_tokens)
        start = time.monotonic()
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=SYSTEM,
                messages=messages,
                tools=TOOLS,
                output_config={"format": OUTPUT_FORMAT},
            )
        except anthropic.APIError as e:
            call["latency_ms"] = int((time.monotonic() - start) * 1000)
            call["error"] = f"API error: {e}"
            db.log_llm_call(call)
            raise PlanGenerationError(call["error"], usage)

        text = record_response(call, response, start, usage)

        if response.stop_reason == "refusal":
            call["error"] = "Claude refused to answer"
            db.log_llm_call(call)
            raise PlanGenerationError(call["error"], usage)

        if response.stop_reason == "max_tokens":
            error = f"output was cut off at max_tokens={max_tokens}"
            call["error"] = error
            db.log_llm_call(call)
            max_tokens = MAX_TOKENS_AFTER_CUTOFF
            continue

        if response.stop_reason == "tool_use":
            db.log_llm_call(call)  # not an error: Claude asked for information
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                usage.tool_calls.append({"name": block.name, "input": block.input})
                result, is_error = tools.run(block.name, block.input, alarm)
                # tool_use_id ties the result to the request; the API rejects a result whose id
                # is not in the assistant turn right before it.
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": result, "is_error": is_error})
            messages = messages + [
                {"role": "assistant", "content": content_as_dicts(response)},  # Claude's own request, sent back
                {"role": "user", "content": results},
            ]
            continue

        # end_turn: the text is the plan.
        try:
            plan = HandlingPlan.model_validate_json(text)
        except ValidationError as e:
            error = str(e)
            call["error"] = error
            db.log_llm_call(call)
            if retries == MAX_RETRIES:
                raise PlanGenerationError(error, usage)
            retries += 1
            messages = messages + [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": f"Your output did not pass validation:\n{error}\n"
                    "Return the complete handling plan again with this fixed.",
                },
            ]
            continue

        db.log_llm_call(call)
        return plan, usage

    raise PlanGenerationError(
        f"max_turns reached: {MAX_TURNS} API calls without a valid plan" + (f" (last error: {error})" if error else ""),
        usage,
    )
