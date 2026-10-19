"""Validate a requested write and park it as a pending action. Shared by REST routes and the agent."""
from __future__ import annotations

import datetime as dt

from app.erp.client import ERPError
from app.services import activities, parties, sales
from app.services.errors import NotFound, ValidationFailed

OPPORTUNITY_STATUSES = ("Open", "Replied", "Closed")
DEFAULT_STAGES = ("Prospecting", "Qualification", "Needs Analysis", "Value Proposition", "Identifying Decision Makers",
                  "Perception Analysis", "Proposal/Price Quote", "Negotiation/Review")


def _record(st, caller, kind, payload, key=None):
    action = st.actions.create(caller, kind, payload, key)
    st.audit.write(caller, "propose", action=action.id, kind=kind)
    return action


def log_activity(st, caller, activity_type, details, status=None, date=None, party_type=None, party_id=None,
                 new_client_name=None, key=None):
    erp = st.erp_for(caller)
    return _record(st, caller, "log_activity", activities.preview(
        erp, activity_type, details, status, date, party_type, party_id, new_client_name), key)


def _require_item_creator(st, caller):
    if caller not in st.settings.item_creators:
        raise PermissionError("This caller may not create new items; ask a manager or use an existing item")


def item(st, caller, name, price, description="", uom=None, item_group=None, code=None, allow_similar=False, key=None,
         ignore_ids=frozenset()):
    _require_item_creator(st, caller)
    spec = {"name": name, "price": price, "description": description, "uom": uom, "item_group": item_group, "code": code,
            "allow_similar": allow_similar}
    return _record(st, caller, "create_item", sales.preview_item(st.erp_for(caller), spec, ignore_ids), key)


def quotation(st, caller, party_type, party_id, lines, valid_till=None, key=None, ignore_ids=frozenset()):
    if any(l.get("new_item") for l in lines):
        _require_item_creator(st, caller)
    return _record(st, caller, "create_document",
                   sales.build_preview(st.erp_for(caller), "Quotation", party_type, party_id, lines, valid_till=valid_till,
                                       ignore_ids=ignore_ids), key)


def order(st, caller, customer_id, lines, delivery_date=None, key=None, ignore_ids=frozenset()):
    if any(l.get("new_item") for l in lines):
        _require_item_creator(st, caller)
    return _record(st, caller, "create_document",
                   sales.build_preview(st.erp_for(caller), "Sales Order", "Customer", customer_id, lines,
                                       delivery_date=delivery_date, ignore_ids=ignore_ids), key)


def submit_document(st, caller, doctype, doc_id, key=None):
    if doctype == "Sales Order" and caller not in st.settings.confirm_callers:
        raise PermissionError("This caller may not confirm Sales Orders")
    return _record(st, caller, "submit_document", sales.preview_submit(st.erp_for(caller), doctype, doc_id), key)


def quotation_to_order(st, caller, quotation_id, delivery_date=None, key=None):
    erp = st.erp_for(caller)
    q = sales.get_document(erp, "Quotation", quotation_id)
    if q.get("docstatus") != 1:
        raise ValidationFailed("The quotation must be submitted before it can become a Sales Order")
    delivery = sales._future_date(delivery_date, dt.date.today() + dt.timedelta(days=14))
    return _record(st, caller, "quotation_to_order", {
        "quotation_id": quotation_id, "party_name": q.get("customer_name"), "grand_total": q.get("grand_total"),
        "currency": q.get("currency"), "delivery_date": delivery}, key)


def create_opportunity(st, caller, party_type, party_id, amount=None, sales_stage=None, expected_closing=None,
                       notes="", key=None):
    erp = st.erp_for(caller)
    if party_type not in ("Lead", "Customer"):
        raise ValidationFailed("party_type must be Lead or Customer")
    payload = {"party_type": party_type, "party_id": party_id, "party_name": parties.party_name(erp, party_type, party_id),
               "notes": (notes or "")[:1000]}
    if amount is not None:
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or not 0 <= amount <= 1_000_000_000:
            raise ValidationFailed("amount must be a positive number")
        payload["amount"] = amount
    if sales_stage:
        payload["sales_stage"] = _stage(erp, sales_stage)
    if expected_closing:
        payload["expected_closing"] = sales._future_date(expected_closing, dt.date.today())
    return _record(st, caller, "create_opportunity", payload, key)


def update_opportunity(st, caller, opportunity_id, sales_stage=None, status=None, amount=None, key=None):
    erp = st.erp_for(caller)
    rows = erp.get_list("Opportunity", [["name", "=", opportunity_id]], ["name", "customer_name", "sales_stage", "status",
                                                                        "opportunity_amount"], 1)
    if not rows:
        raise NotFound(f"Opportunity {opportunity_id} not found")
    changes = {}
    if sales_stage:
        changes["sales_stage"] = _stage(erp, sales_stage)
    if status:
        if status not in OPPORTUNITY_STATUSES:
            raise ValidationFailed(f"status must be one of: {', '.join(OPPORTUNITY_STATUSES)}")
        changes["status"] = status
    if amount is not None:
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or not 0 <= amount <= 1_000_000_000:
            raise ValidationFailed("amount must be a positive number")
        changes["opportunity_amount"] = amount
    if not changes:
        raise ValidationFailed("Nothing to change: give sales_stage, status or amount")
    before = {k: rows[0].get(k) for k in changes}
    return _record(st, caller, "update_opportunity", {"opportunity_id": opportunity_id, "party_name": rows[0].get("customer_name"),
                                                      "changes": changes, "before": before}, key)


def _stage(erp, value: str) -> str:
    try:
        stages = [r["name"] for r in erp.get_list("Sales Stage", [], ["name"], 50)]
    except ERPError:  # role cannot read Sales Stage: fall back to ERPNext's standard stages
        stages = list(DEFAULT_STAGES)
    for s in stages:
        if s.lower() == value.strip().lower():
            return s
    raise ValidationFailed(f"sales_stage must be one of: {', '.join(stages)}")


def mcp_call(st, caller, full_name: str, arguments: dict | None, key=None):
    """Park a non-read-only MCP tool call. It runs only after the user's yes, like every other write."""
    hub = getattr(st, "mcp", None)
    meta = hub.describe(full_name) if hub else None
    if not meta:
        raise NotFound(f"MCP tool {full_name} is not available")
    if not isinstance(arguments, dict):
        raise ValidationFailed("MCP tool arguments must be an object")
    return _record(st, caller, "mcp_call", {"tool_name": full_name, "server": meta["server"], "tool": meta["tool"],
                                            "arguments": arguments}, key)
