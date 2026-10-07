"""The lease record: which fields the agent extracts and how raw values are normalised.

Everything that enters the record, whether from the model or from a human
override, goes through `coerce`, so the rule engine can trust the types.
"""
import math
from datetime import date

# name -> (type, label). Dict order is display order.
FIELDS: dict[str, tuple[str, str]] = {
    "landlord_name": ("text", "Landlord"),
    "tenant_name": ("text", "Tenant"),
    "landlord_signed": ("bool", "Signed by landlord"),
    "tenant_signed": ("bool", "Signed by tenant"),
    "premises": ("text", "Premises as written"),
    "unit_id": ("text", "Matched unit"),
    "commencement_date": ("date", "Commencement date"),
    "expiry_date": ("date", "Expiry date"),
    "term_months": ("number", "Stated term (months)"),
    "currency": ("text", "Currency"),
    "monthly_rent": ("number", "Monthly rent"),
    "annual_rent": ("number", "Annual rent"),
    "rent_frequency": ("text", "Rent frequency"),
    "deposit_amount": ("number", "Security deposit"),
    "escalation_clause": ("text", "Escalation clause"),
    "escalation_defined": ("bool", "Escalation mechanism defined"),
    "renewal_terms": ("text", "Renewal terms"),
    "termination_terms": ("text", "Termination terms"),
}


def coerce(name: str, value):
    """Normalise a value to its field's type.

    Raises ValueError with a message the model or the reviewer can act on.
    """
    if name not in FIELDS:
        raise ValueError(f"Unknown field '{name}'. Valid fields: {', '.join(FIELDS)}.")
    kind = FIELDS[name][0]
    text = str(value).strip()
    if kind == "bool":
        if text.lower() not in ("true", "false"):
            raise ValueError(f"{name} must be true or false.")
        return text.lower() == "true"
    if kind == "number":
        try:
            number = float(text.replace(",", ""))
        except ValueError:
            raise ValueError(f"{name} must be a plain number, with no currency or units.") from None
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"{name} must be a non-negative number.")
        return int(number) if number.is_integer() else number
    if kind == "date":
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            raise ValueError(f"{name} must be an ISO date (YYYY-MM-DD).") from None
    if not text:
        raise ValueError(f"{name} must not be empty.")
    return text
