"""The judge prompt.

judge.py fills JUDGE_USER_TEMPLATE with .format(action=..., sop_steps=...):
  action     = the text of one action in the handling plan
  sop_steps  = every step of each SOP this action cites; the cited steps end with "<- CITED"
The output format (reason, judgement) is set by the Judge class in judge.py, not here.
"""

JUDGE_SYSTEM_PROMPT = """You check one action from an equipment handling plan against the SOP step or steps it cites.
You get the whole SOP so you can read what a step refers to (for example "all of the above"). The cited steps are marked with <- CITED. Judge the action only against the cited steps.
Set judgement to true only if the action does what the cited steps say: the same operation, even if the wording is different.
Set judgement to false if the action does a different operation, leaves out part of the cited steps, or adds an operation that is not in the cited steps.
Extra words that do not add an operation are fine: a reason, a reminder of what to pay attention to, or a value from the alarm (like the reading or the spec limit).
Use only the texts given. Do not use your own knowledge of the equipment.
Write the reason first, in one or two sentences, then the judgement."""

JUDGE_USER_TEMPLATE = """Action:
{action}

SOP text:
{sop_steps}"""
