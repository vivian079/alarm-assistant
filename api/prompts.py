SYSTEM_PROMPT = """This is for an equipment engineer. You need to generate a handling plan, including alarm_summary, likely_causes, actions.
Each action should come from an SOP step. If an action does not come from any SOP step, leave its sop_refs empty. Do not make up SOP IDs or step numbers.
alarm_summary must include: tool ID, chamber, alarm code, sensor reading, spec limit, wafer count, what this tool recently alarmed on and what was repaired.
In each SOP reference, sop_id is the SOP ID (like SOP-012) and step is the step number.
The SOP library is below. Only cite SOPs and steps from this library."""

# Put in front of the SOP list (see llm.load_sop_library).
SOP_LIBRARY_INTRO = """SOP library.
Each SOP lists the alarm codes and process steps it applies to.
Every SOP has this note: if the same tool reported the same alarm code in the last 7 days, do the steps and also notify the equipment engineer to escalate."""

USER_PROMPT_TEMPLATE = """alarm_id: {alarm_id}
timestamp: {timestamp}
tool_id: {tool_id}
chamber: {chamber}
alarm_code: {alarm_code}
process_step: {process_step}
sensor_value: {sensor_value}
spec_lower: {spec_lower}
spec_upper: {spec_upper}
unit: {unit}
wafer_count: {wafer_count}
recent_history: {recent_history}"""
