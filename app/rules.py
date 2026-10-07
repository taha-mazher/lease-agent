"""Deterministic validation of a lease record against the owner's ruleset.

The model extracts facts; this module judges them. Keeping arithmetic and date
maths out of the model means a verdict is reproducible, and it is recomputed
every time a reviewer overrides a field.

Rule text and severity come from owner_ruleset.json. The checks themselves are
code, keyed by rule id. A rule with no checker reports NOT_DETERMINABLE rather
than silently passing.
"""
from calendar import monthrange
from datetime import date, timedelta

from .fields import FIELDS

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "NOT_DETERMINABLE"


def add_months(start: date, months: int) -> date:
    years, month_index = divmod(start.month - 1 + months, 12)
    year, month = start.year + years, month_index + 1
    return date(year, month, min(start.day, monthrange(year, month)[1]))


def months_between(start: date, end: date) -> int:
    """Whole months in a term, counting the expiry day as part of the term.

    1 Jan to 31 Dec is 12 months, and so is 1 Jan to 1 Jan.
    """
    end += timedelta(days=1)
    months = (end.year - start.year) * 12 + end.month - start.month
    return months if end.day >= start.day else months - 1


def _verdict(ok: bool, reason: str) -> tuple[str, str]:
    return (PASS if ok else FAIL), reason


def _deposit(f, _ctx):
    return _verdict(
        f["deposit_amount"] >= f["monthly_rent"],
        f"Deposit {f['deposit_amount']:,} against monthly rent {f['monthly_rent']:,}.",
    )


def _escalation(f, _ctx):
    if f["escalation_defined"]:
        return PASS, "The clause states a concrete mechanism."
    return FAIL, "The clause states no concrete mechanism or percentage."


def _term_limit(f, _ctx):
    term = f.get("term_months")
    if term is None and f.get("commencement_date") and f.get("expiry_date"):
        term = months_between(date.fromisoformat(f["commencement_date"]), date.fromisoformat(f["expiry_date"]))
    if term is None:
        return UNKNOWN, "Neither a stated term nor both dates are available."
    return _verdict(term <= 36, f"Term is {term} months against a limit of 36.")


def _dates(f, _ctx):
    start, end = date.fromisoformat(f["commencement_date"]), date.fromisoformat(f["expiry_date"])
    if end <= start:
        return FAIL, f"Expiry {end} is not after commencement {start}."
    # Leases end either on the anniversary or the day before it. Accept both.
    anniversary = add_months(start, int(f["term_months"]))
    matches = end in (anniversary, anniversary - timedelta(days=1))
    return _verdict(
        matches,
        f"Stated term is {f['term_months']} months. {start} to {end} is {months_between(start, end)} whole months.",
    )


def _parties(f, _ctx):
    unsigned = [party for party in ("landlord", "tenant") if not f[f"{party}_signed"]]
    if unsigned:
        return FAIL, f"Not signed by the {' or the '.join(unsigned)}."
    return PASS, "Both parties are named and have signed."


def _annual_rent(f, _ctx):
    expected = f["monthly_rent"] * 12
    return _verdict(f["annual_rent"] == expected, f"Annual rent {f['annual_rent']:,} against 12 x monthly = {expected:,}.")


def _unit(f, ctx):
    status = ctx["units"].get(f["unit_id"])
    if status is None:
        return FAIL, f"{f['unit_id']} is not in the unit register."
    if ctx["linked_unit"] == f["unit_id"]:
        return PASS, "This lease is the one linked to the unit."
    return _verdict(status == "available", f"The unit is marked '{status}'.")


# rule id -> (check, fields that must be present, fields used if present)
CHECKS = {
    "R1": (_deposit, ("deposit_amount", "monthly_rent"), ()),
    "R2": (_escalation, ("escalation_defined",), ("escalation_clause",)),
    "R3": (_term_limit, (), ("term_months", "commencement_date", "expiry_date")),
    "R4": (_dates, ("commencement_date", "expiry_date", "term_months"), ()),
    "R5": (_parties, ("landlord_name", "tenant_name", "landlord_signed", "tenant_signed"), ()),
    "R6": (_annual_rent, ("annual_rent", "monthly_rent"), ()),
    "R7": (_unit, ("unit_id",), ()),
}


def evaluate(ruleset: dict, facts: dict, units: dict[str, str], linked_unit: str | None = None) -> list[dict]:
    """Judge `facts` (field name -> value, absent or None when unknown) against every rule.

    `units` maps unit id to status. `linked_unit` is the unit this lease already
    occupies, so an active lease does not fail R7 against itself.
    Each result is the rule plus status, reason, and the fields it relied on.
    """
    ctx = {"units": units, "linked_unit": linked_unit}
    results = []
    for rule in ruleset["rules"]:
        check, required, optional = CHECKS.get(rule["id"], (None, (), ()))
        missing = [FIELDS[name][1] for name in required if facts.get(name) is None]
        if check is None:
            status, reason = UNKNOWN, "No checker is implemented for this rule yet."
        elif missing:
            status, reason = UNKNOWN, f"Missing or rejected: {', '.join(missing)}."
        else:
            status, reason = check(facts, ctx)
        results.append({**rule, "status": status, "reason": reason, "fields": [*required, *optional]})
    return results
