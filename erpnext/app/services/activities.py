"""Sales Activity records: the structured log of what the sales team did (stored in ERPNext)."""
from __future__ import annotations

import datetime as dt

from app.erp.client import ERPAPI
from app.services import parties
from app.services.errors import ValidationFailed
from app.services.search import name_filters

DOCTYPE = "Sales Activity"
ACTIVITY_TYPES = ["Meeting", "Call", "Quotation Sent", "BOQ Received", "Order Finalized", "Other"]
STATUSES = ["Open", "Awaiting Client", "In Progress", "Completed"]
_ALIASES = {"quotation": "Quotation Sent", "quote": "Quotation Sent", "quotation sent": "Quotation Sent",
            "boq": "BOQ Received", "boq received": "BOQ Received", "order": "Order Finalized",
            "order finalised": "Order Finalized", "order finalized": "Order Finalized", "visit": "Meeting",
            "site visit": "Meeting", "phone call": "Call"}
_STATUS_ALIASES = {"awaiting": "Awaiting Client", "waiting": "Awaiting Client", "pending": "Awaiting Client",
                   "done": "Completed", "closed": "Completed", "complete": "Completed", "in progress": "In Progress",
                   "ongoing": "In Progress", "open": "Open"}


def _pick(value: str | None, allowed: list[str], aliases: dict[str, str], label: str) -> str:
    v = (value or "").strip()
    for a in allowed:
        if a.lower() == v.lower():
            return a
    if v.lower() in aliases:
        return aliases[v.lower()]
    raise ValidationFailed(f"{label} must be one of: {', '.join(allowed)}")


def normalize_type(value: str | None) -> str:
    return _pick(value, ACTIVITY_TYPES, _ALIASES, "activity_type")


def normalize_status(value: str | None) -> str:
    return _pick(value or "Open", STATUSES, _STATUS_ALIASES, "status")


def parse_date(value: str | None) -> dt.date:
    if not value:
        return dt.date.today()
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ValidationFailed("date must be YYYY-MM-DD")


def list_activities(erp: ERPAPI, limit: int, client: str | None = None, activity_type: str | None = None,
                    status: str | None = None, date_from: dt.date | None = None, date_to: dt.date | None = None,
                    mine: bool = False) -> list[dict]:
    filters: list = name_filters("client_name", client) if client else []
    if activity_type:
        filters.append(["activity_type", "=", normalize_type(activity_type)])
    if status:
        filters.append(["status", "=", normalize_status(status)])
    if date_from:
        filters.append(["activity_date", ">=", date_from.isoformat()])
    if date_to:
        filters.append(["activity_date", "<=", date_to.isoformat()])
    if mine:
        filters.append(["owner", "=", getattr(erp, "user", "") or erp.whoami()])
    return erp.get_list(DOCTYPE, filters, ["name", "client_name", "activity_type", "activity_date", "status", "details",
                                           "reference_doctype", "reference_name", "owner"], limit, "activity_date desc, modified desc")


def preview(erp: ERPAPI, activity_type: str, details: str, status: str | None, date: str | None, party_type: str | None,
            party_id: str | None, new_client_name: str | None) -> dict:
    """Validate an activity and describe it. Either an existing party, or a new client (created as a Lead)."""
    payload = {"activity_type": normalize_type(activity_type), "status": normalize_status(status),
               "date": parse_date(date).isoformat(), "details": (details or "").strip()[:2000]}
    if party_id:
        party_type = party_type or "Customer"
        payload.update(party_type=party_type, party_id=party_id, client_name=parties.party_name(erp, party_type, party_id))
    elif (new_client_name or "").strip():
        payload.update(new_client_name=new_client_name.strip()[:140], client_name=new_client_name.strip()[:140])
    else:
        raise ValidationFailed("Give an existing client (party_id) or new_client_name")
    return payload


def create(erp: ERPAPI, p: dict) -> dict:
    lead_id = None
    ref_type, ref_id = p.get("party_type"), p.get("party_id")
    if p.get("new_client_name"):
        lead = erp.insert("Lead", {"company_name": p["new_client_name"], "lead_name": p["new_client_name"], "status": "Open"})
        ref_type, ref_id, lead_id = "Lead", lead["name"], lead["name"]
    doc = erp.insert(DOCTYPE, {"client_name": p["client_name"], "activity_type": p["activity_type"],
                               "activity_date": p["date"], "status": p["status"], "details": p["details"],
                               "reference_doctype": ref_type, "reference_name": ref_id})
    return {"id": doc["name"], "client_name": p["client_name"], "activity_type": p["activity_type"],
            "status": p["status"], "date": p["date"], "lead_created": lead_id}
