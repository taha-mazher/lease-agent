"""HTTP API. Routes stay thin: agents propose, humans decide, rules are recomputed on read."""
import json
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import db
from .agent import ModelError
from .documents import extract_segments
from .fields import FIELDS, coerce
from .issue_agent import CONDITIONS, PRIORITIES, run_issue_agent
from .lease_agent import run_lease_agent
from .model import get_model
from .rules import FAIL, evaluate

MAX_LEASE_BYTES = 10 * 1024 * 1024
MAX_PHOTO_BYTES = 5 * 1024 * 1024  # the Anthropic API's per-image limit
MAX_PHOTOS = 8
INDEX = Path(__file__).parent / "static" / "index.html"

Decision = Literal["pending", "accepted", "rejected"]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    yield


app = FastAPI(title="Lease and issue agents", lifespan=lifespan)


@app.exception_handler(ModelError)
def model_error(_request, exc: ModelError):
    return JSONResponse({"detail": str(exc)}, status_code=502)


@app.get("/")
def index():
    return FileResponse(INDEX)



def _effective(row) -> Any:
    """The value the record currently stands on: the human's override, else the agent's value, unless rejected."""
    if row is None or row["decision"] == "rejected":
        return None
    return json.loads(row["override"] if row["override"] is not None else row["value"] or "null")


def _unit_statuses(con) -> dict[str, str]:
    return {row["unit_id"]: row["status"] for row in con.execute("SELECT unit_id, status FROM units")}


def _lease_view(con, lease_id: int) -> dict:
    lease = con.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
    if lease is None:
        raise HTTPException(404, "Lease not found.")
    rows = {row["name"]: row for row in con.execute("SELECT * FROM lease_fields WHERE lease_id = ?", (lease_id,))}
    fields = []
    for name, (kind, label) in FIELDS.items():
        row = rows.get(name)
        fields.append({
            "name": name, "kind": kind, "label": label,
            "value": json.loads(row["value"] or "null") if row else None,
            "override": json.loads(row["override"] or "null") if row else None,
            "effective": _effective(row),
            "quote": row["quote"] if row else None,
            "segment": row["segment"] if row else None,
            "cited": bool(row["cited"]) if row else False,
            "decision": row["decision"] if row else "pending",
        })
    facts = {field["name"]: field["effective"] for field in fields}
    flags = [dict(row) for row in con.execute("SELECT * FROM lease_flags WHERE lease_id = ? ORDER BY id", (lease_id,))]
    return {
        "id": lease["id"], "filename": lease["filename"], "status": lease["status"], "unit_id": lease["unit_id"],
        "proposed_unit": facts["unit_id"], "summary": lease["summary"], "model": lease["model"],
        "created_at": lease["created_at"], "fields": fields, "flags": flags,
        "rules": evaluate(db.ruleset(), facts, _unit_statuses(con), lease["unit_id"]),
        "segments": json.loads(lease["segments"]), "trace": json.loads(lease["trace"]),
    }


def _lease_index(con) -> list[dict]:
    """Every lease with the unit it belongs to: the linked unit, else the one currently proposed."""
    # Scans every lease on each call. Store the proposed unit on the lease row when that gets slow.
    rows = con.execute(
        "SELECT l.id, l.filename, l.status, l.unit_id, f.value, f.override FROM leases l "
        "LEFT JOIN lease_fields f ON f.lease_id = l.id AND f.name = 'unit_id' AND f.decision != 'rejected' "
        "ORDER BY l.id DESC")
    return [{"id": r["id"], "filename": r["filename"], "status": r["status"],
             "unit": r["unit_id"] or json.loads(r["override"] or r["value"] or "null")} for r in rows]


def _issue_view(row) -> dict:
    issue = dict(row)
    for column in ("photos", "observations", "work_order", "trace"):
        issue[column] = json.loads(issue[column])
    return issue


def _unit_or_404(con, unit_id: str):
    unit = con.execute("SELECT * FROM units WHERE unit_id = ?", (unit_id,)).fetchone()
    if unit is None:
        raise HTTPException(404, "Unit not found.")
    return unit


@app.get("/api/overview")
def overview():
    with db.connect() as con:
        leases = _lease_index(con)
        pending = dict(con.execute("SELECT unit_id, COUNT(*) FROM issues WHERE decision = 'pending' GROUP BY unit_id").fetchall())
        units = [{**dict(unit), "leases": [l for l in leases if l["unit"] == unit["unit_id"]],
                  "pending_issues": pending.get(unit["unit_id"], 0)}
                 for unit in con.execute("SELECT * FROM units ORDER BY building_name, unit_id")]
    return {"model": get_model().name, "units": units, "unmatched_leases": [l for l in leases if l["unit"] is None],
            "conditions": CONDITIONS, "priorities": PRIORITIES}


@app.get("/api/units/{unit_id}")
def unit_detail(unit_id: str):
    with db.connect() as con:
        unit = dict(_unit_or_404(con, unit_id))
        leases = [_lease_view(con, l["id"]) for l in _lease_index(con) if l["unit"] == unit_id]
        issues = [_issue_view(row) for row in con.execute("SELECT * FROM issues WHERE unit_id = ? ORDER BY id DESC", (unit_id,))]
    return {"unit": unit, "leases": leases, "issues": issues}



@app.post("/api/leases", status_code=201)
def upload_lease(file: UploadFile = File(...)):
    data = file.file.read(MAX_LEASE_BYTES + 1)
    if len(data) > MAX_LEASE_BYTES:
        raise HTTPException(413, "The lease file is larger than 10 MB.")
    try:
        segments = extract_segments(file.filename or "", data)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    with db.connect() as con:
        units = [dict(row) for row in con.execute("SELECT * FROM units")]
    model = get_model()
    draft = run_lease_agent(model, segments, units, db.ruleset())  # slow, so no transaction is held open
    with db.connect() as con:
        lease_id = con.execute(
            "INSERT INTO leases (filename, segments, trace, summary, model) VALUES (?, ?, ?, ?, ?)",
            (file.filename, json.dumps(segments), json.dumps(draft["trace"]), draft["summary"], model.name)).lastrowid
        con.executemany(
            "INSERT INTO lease_fields (lease_id, name, value, quote, segment, cited) VALUES (?, ?, ?, ?, ?, ?)",
            [(lease_id, name, json.dumps(f["value"]), f["quote"], f["segment"], f["cited"]) for name, f in draft["fields"].items()])
        con.executemany(
            "INSERT INTO lease_flags (lease_id, kind, field, message, quote, segment, raised_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(lease_id, f["kind"], f["field"], f["message"], f["quote"], f["segment"], f["raised_by"]) for f in draft["flags"]])
        return _lease_view(con, lease_id)


@app.get("/api/leases/{lease_id}")
def lease_detail(lease_id: int):
    with db.connect() as con:
        return _lease_view(con, lease_id)


class FieldPatch(BaseModel):
    """Send `value` to correct a field (which also accepts it), or `decision` alone to accept or reject it."""
    decision: Decision | None = None
    value: Any = None


@app.patch("/api/leases/{lease_id}/fields/{name}")
def review_field(lease_id: int, name: str, body: FieldPatch):
    if name not in FIELDS:
        raise HTTPException(404, "Unknown field.")
    with db.connect() as con:
        _lease_view(con, lease_id)  # 404 if the lease does not exist
        if body.value is not None:
            try:
                value = coerce(name, body.value)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            if name == "unit_id" and value not in _unit_statuses(con):
                raise HTTPException(422, f"{value} is not in the unit register.")
            con.execute(
                "INSERT INTO lease_fields (lease_id, name, override, decision) VALUES (?, ?, ?, 'accepted') "
                "ON CONFLICT (lease_id, name) DO UPDATE SET override = excluded.override, decision = 'accepted'",
                (lease_id, name, json.dumps(value)))
        elif body.decision:
            changed = con.execute("UPDATE lease_fields SET decision = ? WHERE lease_id = ? AND name = ?",
                                  (body.decision, lease_id, name)).rowcount
            if not changed:
                raise HTTPException(409, "There is no value to decide on. Enter one instead.")
        else:
            raise HTTPException(422, "Send a value or a decision.")
        return _lease_view(con, lease_id)


class DecisionBody(BaseModel):
    decision: Decision


@app.patch("/api/flags/{flag_id}")
def review_flag(flag_id: int, body: DecisionBody):
    with db.connect() as con:
        flag = con.execute("SELECT lease_id FROM lease_flags WHERE id = ?", (flag_id,)).fetchone()
        if flag is None:
            raise HTTPException(404, "Flag not found.")
        con.execute("UPDATE lease_flags SET decision = ? WHERE id = ?", (body.decision, flag_id))
        return _lease_view(con, flag["lease_id"])


class LinkBody(BaseModel):
    acknowledge_failures: bool = False


@app.post("/api/leases/{lease_id}/link")
def link_lease(lease_id: int, body: LinkBody):
    """Make the lease the unit's active lease and mark the unit occupied. Only a human triggers this."""
    with db.connect() as con:
        lease = _lease_view(con, lease_id)
        if lease["status"] == "active":
            raise HTTPException(409, "This lease is already linked.")
        unit_field = next(field for field in lease["fields"] if field["name"] == "unit_id")
        if unit_field["effective"] is None or unit_field["decision"] != "accepted":
            raise HTTPException(409, "Accept the matched unit before linking the lease.")
        blocking = [rule["id"] for rule in lease["rules"] if rule["status"] == FAIL and rule["severity"] == "high"]
        if blocking and not body.acknowledge_failures:
            raise HTTPException(409, {"message": f"High severity rules are failing: {', '.join(blocking)}.", "blocking_rules": blocking})
        unit_id = unit_field["effective"]
        con.execute("UPDATE leases SET status = 'active', unit_id = ? WHERE id = ?", (unit_id, lease_id))
        con.execute("UPDATE units SET status = 'occupied' WHERE unit_id = ?", (unit_id,))
        return _lease_view(con, lease_id)



def _sniff_image(data: bytes) -> tuple[str, str] | None:
    """(media type, extension) from the file's magic bytes. The client's content type is not trusted."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data.startswith(b"GIF8"):
        return "image/gif", "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def _unit_context(con, unit) -> dict:
    """What the issue agent may know about the unit: this is where the lease informs the work order."""
    active = con.execute("SELECT id FROM leases WHERE unit_id = ? AND status = 'active' ORDER BY id DESC", (unit["unit_id"],)).fetchone()
    lease = None
    if active:
        facts = {field["name"]: field["effective"] for field in _lease_view(con, active["id"])["fields"]}
        lease = {name: facts[name] for name in ("tenant_name", "commencement_date", "expiry_date", "termination_terms")}
    open_issues = [{"id": row["id"], "title": json.loads(row["work_order"])["title"], "decision": row["decision"]}
                   for row in con.execute("SELECT id, work_order, decision FROM issues WHERE unit_id = ? AND decision != 'rejected'", (unit["unit_id"],))]
    return {"unit": dict(unit), "active_lease": lease, "open_issues": open_issues}


@app.post("/api/units/{unit_id}/issues", status_code=201)
def report_issue(unit_id: str, photos: list[UploadFile] = File(...), note: str = Form(""),
                 reporter: Literal["tenant", "inspector"] = Form("tenant")):
    if len(photos) > MAX_PHOTOS:
        raise HTTPException(422, f"Upload at most {MAX_PHOTOS} photos.")
    images = []
    for photo in photos:
        data = photo.file.read(MAX_PHOTO_BYTES + 1)
        kind = _sniff_image(data)
        if len(data) > MAX_PHOTO_BYTES or kind is None:
            raise HTTPException(422, f"{photo.filename}: photos must be JPEG, PNG, GIF or WebP and under 5 MB.")
        images.append({"name": photo.filename or "photo", "media_type": kind[0], "data": data, "file": f"{uuid.uuid4().hex}.{kind[1]}"})
    with db.connect() as con:
        context = _unit_context(con, _unit_or_404(con, unit_id))
    model = get_model()
    result = run_issue_agent(model, context, reporter, note.strip(), images)
    for image in images:
        (db.uploads_dir() / image["file"]).write_bytes(image["data"])
    with db.connect() as con:
        issue_id = con.execute(
            "INSERT INTO issues (unit_id, reporter, note, photos, observations, condition, condition_reason, work_order, trace, summary, model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (unit_id, reporter, note.strip(), json.dumps([{"file": i["file"], "name": i["name"]} for i in images]),
             json.dumps(result["observations"]), result["condition"], result["condition_reason"],
             json.dumps(result["work_order"]), json.dumps(result["trace"]), result["summary"], model.name)).lastrowid
        return _issue_view(con.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone())


class WorkOrder(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=5000)
    priority: Literal[PRIORITIES]  # type: ignore[valid-type]
    trade: str = Field(max_length=100)


class IssuePatch(BaseModel):
    """Any of: a decision on the work order, a corrected condition, an edited work order."""
    decision: Decision | None = None
    condition: Literal[CONDITIONS] | None = None  # type: ignore[valid-type]
    work_order: WorkOrder | None = None


@app.patch("/api/issues/{issue_id}")
def review_issue(issue_id: int, body: IssuePatch):
    changes = {"decision": body.decision, "condition": body.condition,
               "work_order": body.work_order and json.dumps(body.work_order.model_dump())}
    changes = {column: value for column, value in changes.items() if value is not None}
    with db.connect() as con:
        if changes:
            assignments = ", ".join(f"{column} = ?" for column in changes)  # column names are ours, never user input
            con.execute(f"UPDATE issues SET {assignments} WHERE id = ?", (*changes.values(), issue_id))
        row = con.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Issue not found.")
        return _issue_view(row)


@app.get("/uploads/{name}")
def uploaded_photo(name: str):
    if not re.fullmatch(r"[0-9a-f]{32}\.(jpg|png|gif|webp)", name) or not (db.uploads_dir() / name).is_file():
        raise HTTPException(404, "Not found.")
    return FileResponse(db.uploads_dir() / name)
