from datetime import date

import pytest

from app.fields import coerce
from app.rules import FAIL, PASS, UNKNOWN, evaluate, months_between

RULESET = {"rules": [{"id": rule_id, "description": "", "severity": "high"} for rule_id in ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R99")]}
UNITS = {"U1": "available", "U2": "occupied"}
GOOD = {
    "landlord_name": "Owner", "tenant_name": "Tenant", "landlord_signed": True, "tenant_signed": True,
    "unit_id": "U1", "commencement_date": "2026-01-01", "expiry_date": "2026-12-31", "term_months": 12,
    "monthly_rent": 1000, "annual_rent": 12000, "deposit_amount": 1000, "escalation_defined": True,
}


def statuses(facts, linked_unit=None):
    return {rule["id"]: rule["status"] for rule in evaluate(RULESET, facts, UNITS, linked_unit)}


def test_a_clean_lease_passes_every_implemented_rule():
    result = statuses(GOOD)
    assert result.pop("R99") == UNKNOWN  # a rule nobody has written a checker for must not pass silently
    assert set(result.values()) == {PASS}


@pytest.mark.parametrize("change, rule", [
    ({"deposit_amount": 999}, "R1"),
    ({"escalation_defined": False}, "R2"),
    ({"term_months": 37}, "R3"),
    ({"expiry_date": "2027-01-31"}, "R4"),      # 13 months of dates against a stated 12
    ({"expiry_date": "2025-12-31"}, "R4"),      # expiry before commencement
    ({"tenant_signed": False}, "R5"),
    ({"annual_rent": 11000}, "R6"),
    ({"unit_id": "U2"}, "R7"),                  # occupied
    ({"unit_id": "nope"}, "R7"),                # not in the register
])
def test_each_rule_fails_on_its_own_violation(change, rule):
    assert statuses({**GOOD, **change})[rule] == FAIL


def test_missing_facts_are_not_determinable_rather_than_failed():
    result = statuses({**GOOD, "deposit_amount": None, "tenant_signed": None})
    assert result["R1"] == UNKNOWN and result["R5"] == UNKNOWN


def test_term_limit_falls_back_to_the_dates_when_no_term_is_stated():
    facts = {**GOOD, "term_months": None, "expiry_date": "2029-12-31"}
    assert statuses(facts)["R3"] == FAIL


def test_expiry_on_the_anniversary_also_matches_the_stated_term():
    assert statuses({**GOOD, "expiry_date": "2027-01-01"})["R4"] == PASS


def test_a_linked_lease_does_not_fail_occupancy_against_itself():
    assert statuses({**GOOD, "unit_id": "U2"}, linked_unit="U2")["R7"] == PASS


def test_months_between_counts_the_expiry_day():
    assert months_between(date(2026, 3, 1), date(2028, 2, 29)) == 24
    assert months_between(date(2026, 3, 1), date(2028, 3, 31)) == 25


def test_coerce_normalises_and_rejects():
    assert coerce("monthly_rent", "9,500") == 9500
    assert coerce("tenant_signed", "false") is False
    assert coerce("expiry_date", "2028-03-31") == "2028-03-31"
    for name, bad in (("monthly_rent", "QAR 9,500"), ("monthly_rent", True), ("expiry_date", "31 March 2028"), ("nope", 1)):
        with pytest.raises(ValueError):
            coerce(name, bad)
