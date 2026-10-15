"""Executes a confirmed pending action against Odoo. Single place that performs writes."""
from __future__ import annotations

import datetime as dt

from app.services import calendar, crm, sales
from app.services.errors import ValidationFailed
from app.store.actions import PendingAction


def execute(state, action: PendingAction) -> dict:
    odoo, settings = state.odoo, state.settings
    p = action.payload
    if action.kind == "create_order":
        return sales.create_draft_order(odoo, p)
    if action.kind == "create_product":
        return sales.create_product(odoo, p)
    if action.kind == "confirm_order":
        return sales.confirm_order(odoo, p["order_id"])
    if action.kind == "log_activity":
        when = dt.date.fromisoformat(p["date"]) if p.get("date") else dt.date.today()
        return {"activity_id": crm.log_activity(odoo, p["lead_id"], p["summary"], p["note"], when)}
    if action.kind == "create_event":
        uid = odoo.uid or odoo.authenticate()
        return calendar.create_event(odoo, settings, uid, p["name"], p["preview"], p.get("description", ""))
    raise ValidationFailed(f"Unknown action kind {action.kind}")
