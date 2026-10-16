"""Executes a confirmed pending action against ERPNext. Single place that performs writes."""
from __future__ import annotations

from app.services import activities, sales
from app.services.errors import ValidationFailed
from app.store.actions import PendingAction


def execute(state, action: PendingAction) -> dict:
    erp = state.erp_for(action.caller)  # always the caller's own ERPNext user
    p = action.payload
    if action.kind == "log_activity":
        return activities.create(erp, p)
    if action.kind == "create_item":
        return sales.create_item(erp, p)
    if action.kind == "create_document":
        return sales.create_document(erp, p)
    if action.kind == "submit_document":
        return sales.submit(erp, p["doctype"], p["id"])
    if action.kind == "quotation_to_order":
        return sales.quotation_to_order(erp, p["quotation_id"], p["delivery_date"])
    if action.kind == "create_opportunity":
        doc = {"opportunity_from": p["party_type"], "party_name": p["party_id"], "status": "Open", "opportunity_type": "Sales"}
        for src, dst in (("amount", "opportunity_amount"), ("sales_stage", "sales_stage"), ("expected_closing", "expected_closing")):
            if p.get(src) is not None:
                doc[dst] = p[src]
        saved = erp.insert("Opportunity", doc)
        if p.get("notes"):
            me = getattr(erp, "user", "") or erp.whoami()
            erp.call("frappe.desk.form.utils.add_comment", reference_doctype="Opportunity", reference_name=saved["name"],
                     content=p["notes"], comment_email=me, comment_by=me)
        return {"id": saved["name"], "party_name": p["party_name"], "status": saved.get("status"),
                "sales_stage": saved.get("sales_stage"), "amount": saved.get("opportunity_amount")}
    if action.kind == "update_opportunity":
        saved = erp.update("Opportunity", p["opportunity_id"], p["changes"])
        return {"id": saved["name"], "party_name": p["party_name"], "status": saved.get("status"),
                "sales_stage": saved.get("sales_stage"), "amount": saved.get("opportunity_amount")}
    raise ValidationFailed(f"Unknown action kind {action.kind}")
