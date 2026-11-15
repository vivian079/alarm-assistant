"""The judge prompt.

judge.py fills JUDGE_USER_TEMPLATE with .format(action=..., sop_steps=...):
  action     = the text of one action in the handling plan
  sop_steps  = the original text of every SOP step this action cites, one per line
The output format (reason, judgement) is set by the Judge class in judge.py, not here.
"""

JUDGE_SYSTEM_PROMPT = """You check one action from an equipment handling plan against the SOP step or steps it cites.
Set judgement to true only if the action does what the cited SOP text says: the same operation, even if the wording is different.
Set judgement to false if the action does a different operation, leaves out part of the cited SOP text, or adds an operation that is not in the cited SOP text.
Use only the texts given. Do not use your own knowledge of the equipment.
Write the reason first, in one or two sentences, then the judgement."""

JUDGE_USER_TEMPLATE = """Action:
{action}

Cited SOP text:
{sop_steps}"""
