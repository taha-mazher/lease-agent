"""End to end through the HTTP API with the stub model: lease in, review, link, issue on the same unit."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.model import get_model

LEASE = Path(__file__).parent.parent / "samples" / "synthetic_lease.txt"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
UNIT = "MC-B-1204"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODEL_PROVIDER", "stub")
    get_model.cache_clear()
    with TestClient(app) as test_client:
        yield test_client


def upload_lease(client):
    response = client.post("/api/leases", files={"file": (LEASE.name, LEASE.read_bytes(), "text/plain")})
    assert response.status_code == 201, response.text
    return response.json()


def by_name(lease):
    return {field["name"]: field for field in lease["fields"]}


def rule_statuses(lease):
    return {rule["id"]: rule["status"] for rule in lease["rules"]}


def test_lease_is_extracted_cited_validated_and_matched(client):
    lease = upload_lease(client)
    fields = by_name(lease)

    assert fields["monthly_rent"]["value"] == 9500 and fields["deposit_amount"]["value"] == 9000
    assert fields["commencement_date"]["value"] == "2026-03-01" and fields["expiry_date"]["value"] == "2028-03-31"
    assert fields["tenant_signed"]["value"] is False and fields["landlord_signed"]["value"] is True
    assert lease["proposed_unit"] == UNIT
    assert all(field["cited"] for field in fields.values()), "every extracted value must point at real text"
    assert rule_statuses(lease) == {"R1": "FAIL", "R2": "FAIL", "R3": "PASS", "R4": "FAIL", "R5": "FAIL", "R6": "PASS", "R7": "PASS"}
    assert [flag["kind"] for flag in lease["flags"]] == ["contradiction"]  # 9,500 in the clause, 9,800 in the schedule


def test_a_human_override_changes_the_verdict_and_a_rejection_withdraws_the_fact(client):
    lease = upload_lease(client)
    url = f"/api/leases/{lease['id']}/fields"

    corrected = client.patch(f"{url}/deposit_amount", json={"value": "9,500"}).json()
    field = by_name(corrected)["deposit_amount"]
    assert (field["value"], field["override"], field["decision"]) == (9000, 9500, "accepted")  # the agent's value is kept
    assert rule_statuses(corrected)["R1"] == "PASS"

    rejected = client.patch(f"{url}/annual_rent", json={"decision": "rejected"}).json()
    assert rule_statuses(rejected)["R6"] == "NOT_DETERMINABLE"

    assert client.patch(f"{url}/deposit_amount", json={"value": "lots"}).status_code == 422
    assert client.patch(f"{url}/unit_id", json={"value": "MC-Z-0000"}).status_code == 422


def test_linking_needs_a_human_and_updates_occupancy(client):
    lease = upload_lease(client)
    link = f"/api/leases/{lease['id']}/link"

    assert client.post(link, json={}).status_code == 409  # the unit match has not been accepted
    client.patch(f"/api/leases/{lease['id']}/fields/unit_id", json={"decision": "accepted"})
    blocked = client.post(link, json={})
    assert blocked.status_code == 409 and "R1" in blocked.json()["detail"]["blocking_rules"]
    assert unit_status(client) == "available"

    linked = client.post(link, json={"acknowledge_failures": True}).json()
    assert linked["status"] == "active" and unit_status(client) == "occupied"
    assert rule_statuses(linked)["R7"] == "PASS"

    # A second lease for the same unit now fails R7.
    assert rule_statuses(upload_lease(client))["R7"] == "FAIL"


def unit_status(client):
    return client.get(f"/api/units/{UNIT}").json()["unit"]["status"]


def test_an_issue_lands_on_the_unit_next_to_its_lease(client):
    upload_lease(client)
    report = client.post(f"/api/units/{UNIT}/issues", data={"note": "AC is leaking water onto the floor", "reporter": "tenant"},
                         files=[("photos", ("ac_unit.png", PNG, "image/png")), ("photos", ("floor.png", PNG, "image/png"))])
    assert report.status_code == 201, report.text
    issue = report.json()
    assert issue["condition"] == "damaged" and issue["work_order"]["trade"] == "HVAC"
    assert {obs["photo"] for obs in issue["observations"]} == {1, 2}
    assert client.get(f"/uploads/{issue['photos'][0]['file']}").content == PNG

    unit = client.get(f"/api/units/{UNIT}").json()
    assert [l["filename"] for l in unit["leases"]] == [LEASE.name] and [i["id"] for i in unit["issues"]] == [issue["id"]]
    overview = {u["unit_id"]: u for u in client.get("/api/overview").json()["units"]}
    assert overview[UNIT]["pending_issues"] == 1 and overview["MC-A-0301"]["pending_issues"] == 0

    edited = client.patch(f"/api/issues/{issue['id']}", json={
        "decision": "accepted", "condition": "worn",
        "work_order": {**issue["work_order"], "title": "Service AC drain line", "priority": "urgent"}}).json()
    assert (edited["decision"], edited["condition"], edited["work_order"]["title"]) == ("accepted", "worn", "Service AC drain line")
    assert client.patch(f"/api/issues/{issue['id']}", json={"condition": "sparkling"}).status_code == 422


def test_bad_uploads_are_refused(client):
    assert client.post("/api/leases", files={"file": ("lease.exe", b"MZ", "application/octet-stream")}).status_code == 422
    not_an_image = client.post(f"/api/units/{UNIT}/issues", files=[("photos", ("x.png", b"<html>", "image/png"))])
    assert not_an_image.status_code == 422
    assert client.post("/api/units/NOPE/issues", files=[("photos", ("x.png", PNG, "image/png"))]).status_code == 404
    assert client.get("/uploads/..%2Fapp.db").status_code == 404
