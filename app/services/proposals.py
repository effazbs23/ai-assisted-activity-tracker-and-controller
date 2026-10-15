"""Validate a requested write and park it as a pending action. Shared by REST routes and the agent."""
from __future__ import annotations

import datetime as dt

from app.services import calendar, sales
from app.services.errors import NotFound, ValidationFailed


def _record(st, caller, kind, payload, key=None):
    action = st.actions.create(caller, kind, payload, key)
    st.audit.write(caller, "propose", action=action.id, kind=kind)
    return action


def order(st, caller, customer_id: int, lines: list[dict], key=None, ignore_ids=frozenset()):
    return _record(st, caller, "create_order", sales.build_preview(st.odoo, customer_id, lines, ignore_ids), key)


def confirm_order(st, caller, order_id: int):
    if caller not in st.settings.confirm_callers:
        raise PermissionError("This caller may not confirm sales orders")
    rows = st.odoo.search_read("sale.order", [["id", "=", order_id]], ["id", "name", "state"], limit=1)
    if not rows:
        raise NotFound(f"Order {order_id} not found")
    return _record(st, caller, "confirm_order", {"order_id": order_id, "name": rows[0]["name"]})


def activity(st, caller, lead_id: int, summary: str, note: str = "", date: str | None = None, key=None):
    if not st.odoo.search_read("crm.lead", [["id", "=", lead_id]], ["id"], limit=1):
        raise NotFound(f"Lead {lead_id} not found")
    if date:
        try:
            dt.date.fromisoformat(date)
        except ValueError:
            raise ValidationFailed("date must be YYYY-MM-DD")
    return _record(st, caller, "log_activity", {"lead_id": lead_id, "summary": summary, "note": note, "date": date}, key)


def event(st, caller, name: str, start, duration_min: int, attendee_ids: list[int], description: str = "", key=None):
    uid = st.odoo.uid or st.odoo.authenticate()
    me = calendar.organizer_partner_id(st.odoo, uid)
    try:
        start = calendar.parse_local(st.settings, start)
    except ValueError:
        raise ValidationFailed("start must be an ISO datetime like 2026-10-16T14:00")
    preview = calendar.validate_event(st.odoo, st.settings, me, attendee_ids, start, duration_min)
    return _record(st, caller, "create_event", {"name": name, "description": description, "preview": preview}, key)


def product(st, caller, name: str, price, description: str = "", default_code: str = "", key=None,
            allow_similar: bool = False, ignore_ids=frozenset()):
    clean = sales.preview_product(st.odoo, {"name": name, "price": price, "description": description,
                                            "default_code": default_code, "allow_similar": allow_similar}, ignore_ids)
    return _record(st, caller, "create_product", clean, key)
