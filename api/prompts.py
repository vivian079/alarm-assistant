SYSTEM_PROMPT = """This is for an equipment engineer. You need to generate a handling plan, including alarm_summary, likely_causes, actions, flags.
Each action needs an existing SOP attached.
alarm_summary must include: tool ID, chamber, alarm code, sensor reading, spec limit, wafer count, what this tool recently alarmed on and what was repaired.
Each SOP reference must be the SOP ID and step number, like SOP-XXX step N.
flags must list which of these apply: WARNING, repeat alarm, not covered by SOP."""

USER_PROMPT_TEMPLATE = """alarm_id: {alarm_id}
timestamp: {timestamp}
tool_id: {tool_id}
chamber: {chamber}
alarm_code: {alarm_code}
process_step: {process_step}
sensor_value: {sensor_value}
wafer_count: {wafer_count}
recent_history: {recent_history}"""
