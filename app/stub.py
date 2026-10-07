"""A stand-in model so the system runs with no API key.

It plays the same role as a real model: it sees the same prompt and tools and
answers with tool calls, so the loop, the tools, citation checking, rule
validation and review all run exactly as they would in production. What it
cannot do is read. Lease extraction is pattern matching tuned to the sample
lease, and photo assessment works from the reporter's note and the file names
because the stub does not look at pixels. Treat its output as plumbing, not
judgement.
"""
import json
import re
from datetime import datetime

from .agent import Tool, ToolCall, Turn
from .documents import parse_segments

_MONEY = r"([A-Z]{3})\s*([\d,]+(?:\.\d+)?)"
_DATE = r"(\d{1,2} [A-Za-z]+ \d{4})"

# keyword in the note or file name -> (item, trade)
_EQUIPMENT = {
    r"\bac\b|a/c|air.?con": ("AC unit", "HVAC"),
    r"heater|boiler": ("Water heater", "plumbing"),
    r"leak|pipe|tap|sink|faucet|drain|toilet": ("Plumbing fixture", "plumbing"),
    r"fridge|refrigerator|oven|stove|cooker|washer|washing|dishwasher": ("Kitchen appliance", "appliance repair"),
    r"light|socket|switch|wiring|electric": ("Electrical fitting", "electrical"),
    r"wall|ceiling|paint|floor|tile|door|window": ("Interior finish", "general"),
}
_DAMAGED = r"leak|crack|broken|burst|stain|mould|mold|rust|not working|no cooling|damage|flood|spark"
_WORN = r"worn|old|peeling|faded|loose|noisy|slow"
_URGENT = r"flood|burst|spark|no power|gas"


class StubModel:
    name = "stub"

    def turn(self, system: str, messages: list[dict], tools: list[Tool]) -> Turn:
        step = sum(message["role"] == "assistant" for message in messages)
        policy = _lease_turn if any(tool.name == "record_field" for tool in tools) else _issue_turn
        planned, text = policy(step, messages)
        calls = [ToolCall(f"stub_{step}_{i}", name, args) for i, (name, args) in enumerate(planned)]
        content = [{"type": "text", "text": text}] if text else []
        content += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args} for c in calls]
        return Turn(content, calls, text)


def _prompt_text(messages: list[dict]) -> str:
    return "\n".join(block["text"] for block in messages[0]["content"] if block["type"] == "text")


def _last_result(messages: list[dict]):
    """The parsed result of the final tool call in the previous turn."""
    content = messages[-1]["content"][-1]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return content



def _lease_turn(step: int, messages: list[dict]):
    fields, flags = _extract(parse_segments(_prompt_text(messages)))
    premises = next((f for f in fields if f["name"] == "premises"), None)
    if step == 0:
        calls = [("record_field", f) for f in fields] + [("raise_flag", f) for f in flags]
        if premises:
            calls.append(("search_units", {"query": premises["value"]}))
        return calls, ""
    if step == 1:
        calls = []
        matches = _last_result(messages) if premises else []
        clear_winner = matches and (len(matches) == 1 or matches[0]["matched_terms"] > matches[1]["matched_terms"])
        if clear_winner:
            calls.append(("record_field", {**premises, "name": "unit_id", "value": matches[0]["unit_id"]}))
        elif premises:
            calls.append(("raise_flag", {"kind": "suspicious", "field": "unit_id", "quote": premises["quote"],
                                         "message": "The premises do not clearly match one unit in the register."}))
        return calls + [("check_rules", {})], ""
    return [], "Lease read, matched against the unit register and checked against the owner rules."


def _extract(segments: list[dict]) -> tuple[list[dict], list[dict]]:
    fields, flags = [], []

    def find(pattern: str):
        for segment in segments:
            if match := re.search(pattern, segment["text"], re.I):
                return match, segment["id"]
        return None, None

    def add(name: str, pattern: str, value=lambda m: m.group(1)):
        match, segment = find(pattern)
        if not match:
            return
        try:
            fields.append({"name": name, "value": value(match), "quote": match.group(0), "segment": segment})
        except ValueError:
            pass  # matched text that does not parse, for example an unknown month name

    def iso(match):
        return datetime.strptime(match.group(1), "%d %B %Y").date().isoformat()

    add("landlord_name", r"between (.+?) \((?:the )?\W?Landlord")
    add("tenant_name", r"\band (.+?) \((?:the )?\W?Tenant")
    add("premises", r"(?:Apartment|Unit|Flat) \w+(?:, Tower \w+)?", lambda m: m.group(0))
    add("commencement_date", rf"commenc\w* on {_DATE}", iso)
    add("expiry_date", rf"expir\w* on {_DATE}", iso)
    add("term_months", r"term of [^.]*?(\d+)\)? months")
    add("annual_rent", rf"annual rent\D{{0,15}}{_MONEY}", lambda m: m.group(2))
    add("rent_frequency", r"payable (monthly|quarterly|annually|yearly)", lambda m: m.group(1).lower())
    add("deposit_amount", rf"deposit of {_MONEY}", lambda m: m.group(2))

    rents = [(m, s["id"]) for s in segments for m in re.finditer(rf"monthly rent\D{{0,15}}{_MONEY}", s["text"], re.I)]
    if rents:
        first, segment = rents[0]
        fields.append({"name": "monthly_rent", "value": first.group(2), "quote": first.group(0), "segment": segment})
        fields.append({"name": "currency", "value": first.group(1), "quote": first.group(0), "segment": segment})
        for other, _ in rents[1:]:
            if other.group(2) != first.group(2):
                flags.append({"kind": "contradiction", "field": "monthly_rent", "quote": other.group(0),
                              "message": f"Monthly rent is stated as {first.group(2)} in one place and {other.group(2)} in another."})

    for name, heading in (("escalation_clause", "ESCALATION"), ("renewal_terms", "RENEWAL"), ("termination_terms", "TERMINATION")):
        clause = next((s for s in segments if re.match(rf"[\d. ]*[A-Z ]*{heading}", s["text"])), None)
        if clause:
            fields.append({"name": name, "value": clause["text"], "quote": clause["text"], "segment": clause["id"]})
            if name == "escalation_clause":
                defined = bool(re.search(r"\d\s*%|per ?cent|CPI|index", clause["text"], re.I))
                fields.append({"name": "escalation_defined", "value": defined, "quote": clause["text"], "segment": clause["id"]})

    for party in ("Landlord", "Tenant"):
        add(f"{party.lower()}_signed", rf"Signed (?:for|by) the {party}:\s*(.*?)\s*Date", lambda m: bool(re.search(r"[A-Za-z]", m.group(1))))
    return fields, flags



def _issue_turn(step: int, messages: list[dict]):
    if step == 0:
        return [("get_unit_context", {})], ""
    if step > 1:
        return [], "Drafted from the reporter's note and the file names. Photos are not analysed in demo mode."

    prompt = _prompt_text(messages)
    note = re.search(r"Reporter's note: (.*)", prompt).group(1)
    photos = re.findall(r"^Photo (\d+): (.*)$", prompt, re.M)
    context = json.loads(messages[2]["content"][0]["content"])

    calls, trades = [], []
    for number, filename in photos:
        evidence = f"{note} {filename}"
        condition = "damaged" if re.search(_DAMAGED, evidence, re.I) else "worn" if re.search(_WORN, evidence, re.I) else "unclear"
        seen = [(item, trade) for pattern, (item, trade) in _EQUIPMENT.items() if re.search(pattern, evidence, re.I)]
        for item, trade in seen or [("Unidentified item", "general")]:
            trades.append(trade)
            calls.append(("record_observation", {"photo": int(number), "item": item, "condition": condition,
                                                 "damage": note if condition == "damaged" else None}))
    conditions = [args["condition"] for _, args in calls]
    overall = next((c for c in ("damaged", "worn") if c in conditions), "unclear")
    calls.append(("record_assessment", {"condition": overall, "reasoning": "Inferred from the reporter's note and the file names. Photos are not analysed in demo mode."}))

    item = calls[0][1]["item"]
    title = f"Inspect {item.lower()}" if note == "(none)" else note[:80] if note.lower().startswith(item.lower()) else f"{item}: {note[:60]}"
    description = note
    duplicate = next((issue for issue in context["open_issues"] if issue["title"] == title), None)
    if duplicate:
        description += f" Possible duplicate of open issue #{duplicate['id']}."
    priority = "urgent" if re.search(_URGENT, note, re.I) else "high" if overall == "damaged" else "medium"
    calls.append(("draft_work_order", {"title": title, "description": description, "priority": priority, "trade": trades[0]}))
    return calls, ""
