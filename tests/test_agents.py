"""The agent loop and the lease agent's guard rails, driven by scripted models."""
import io
import zipfile

import pytest

from app.agent import Tool, ToolCall, Turn, run_agent
from app.documents import extract_segments, locate_quote
from app.lease_agent import run_lease_agent

SEGMENTS = [{"id": "s1", "page": None, "text": "The monthly rent is QAR 9,500."},
            {"id": "s2", "page": None, "text": "The Tenant shall pay a deposit of QAR 9,000."}]
UNITS = [{"unit_id": "U1", "label": "Apartment 1", "status": "available"}]
RULESET = {"rules": [{"id": "R1", "description": "", "severity": "high"}]}


class Scripted:
    """A model that plays back a fixed list of turns and records what it was shown."""
    name = "scripted"

    def __init__(self, *turns):
        self.turns, self.seen = list(turns), []

    def turn(self, system, messages, tools):
        self.seen.append(messages[-1]["content"])
        calls = [ToolCall(f"c{i}", name, args) for i, (name, args) in enumerate(self.turns.pop(0))] if self.turns else []
        return Turn([], calls, "" if calls else "done")


def test_tool_errors_go_back_to_the_model_instead_of_crashing():
    def double(n):
        if n < 0:
            raise ValueError("n must not be negative")
        return {"result": n * 2}

    model = Scripted([("double", {"n": -1}), ("missing_tool", {})], [("double", {"n": 2})])
    trace, summary = run_agent(model, "", [], [Tool("double", "", {}, double)])

    assert [(step["tool"], step["error"]) for step in trace] == [("double", True), ("missing_tool", True), ("double", False)]
    assert model.seen[1][0]["content"] == "Error: n must not be negative" and model.seen[1][0]["is_error"]
    assert summary == "done"


def test_the_loop_stops_at_the_step_limit():
    model = Scripted(*[[("noop", {})]] * 10)
    trace, summary = run_agent(model, "", [], [Tool("noop", "", {}, lambda: "ok")], max_steps=3)
    assert len(trace) == 3 and "step limit" in summary


def test_an_invented_quote_is_kept_but_marked_unverified():
    model = Scripted([
        ("record_field", {"name": "monthly_rent", "value": 9500, "quote": "monthly rent is QAR 9,500", "segment": "s1"}),
        ("record_field", {"name": "deposit_amount", "value": 19000, "quote": "a deposit of QAR 19,000", "segment": "s2"}),
        ("record_field", {"name": "unit_id", "value": "U9", "quote": "x", "segment": "s1"}),
    ])
    draft = run_lease_agent(model, SEGMENTS, UNITS, RULESET)

    assert draft["fields"]["monthly_rent"]["cited"] is True
    assert draft["fields"]["deposit_amount"]["cited"] is False
    assert "unit_id" not in draft["fields"]  # a unit outside the register is refused outright
    kinds = {(flag["kind"], flag["field"]) for flag in draft["flags"]}
    assert ("unverified", "deposit_amount") in kinds and ("missing", "tenant_name") in kinds


def test_quote_matching_ignores_case_and_spacing_and_fixes_the_segment():
    assert locate_quote(SEGMENTS, "the  MONTHLY rent", "s2") == "s1"
    assert locate_quote(SEGMENTS, "QAR 9,600") is None
    assert locate_quote(SEGMENTS, "") is None


def test_docx_and_unsupported_uploads():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", '<w:document><w:p><w:r><w:t>Rent &amp; </w:t></w:r><w:r><w:t xml:space="preserve">deposit</w:t></w:r></w:p><w:p></w:p></w:document>')
    assert extract_segments("lease.docx", buffer.getvalue()) == [{"id": "s1", "page": None, "text": "Rent & deposit"}]
    for name, data in (("lease.exe", b"x"), ("lease.docx", b"not a zip"), ("lease.txt", b"   ")):
        with pytest.raises(ValueError):
            extract_segments(name, data)
