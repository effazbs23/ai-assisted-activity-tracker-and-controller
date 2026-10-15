from __future__ import annotations

import datetime as dt

from app.odoo.client import OdooAPI
from app.services.errors import NotFound
from app.services.search import fuzzy_find, name_domain


def search_customers(odoo: OdooAPI, q: str, limit: int) -> list[dict]:
    fields = ["id", "name", "email", "phone"]
    rows = odoo.search_read("res.partner", name_domain("name", q), fields, limit=limit, order="name")
    return rows or fuzzy_find(odoo, "res.partner", "name", q, fields, limit=min(limit, 3))


def search_leads(odoo: OdooAPI, q: str | None, limit: int) -> list[dict]:
    """Match the lead's own name first; if nothing, match by customer name ('Gulf Interiors LLC')."""
    fields = ["id", "name", "partner_id", "stage_id", "expected_revenue", "user_id"]
    if not q:
        return odoo.search_read("crm.lead", [], fields, limit=limit, order="id desc")
    return (odoo.search_read("crm.lead", name_domain("name", q), fields, limit=limit, order="id desc")
            or odoo.search_read("crm.lead", name_domain("partner_id.name", q), fields, limit=limit, order="id desc")
            or fuzzy_find(odoo, "crm.lead", "name", q, fields, limit=min(limit, 3)))


def log_activity(odoo: OdooAPI, lead_id: int, summary: str, note: str, date: dt.date) -> int:
    if not odoo.search_read("crm.lead", [["id", "=", lead_id]], ["id"], limit=1):
        raise NotFound(f"Lead {lead_id} not found")
    model = odoo.search_read("ir.model", [["model", "=", "crm.lead"]], ["id"], limit=1)[0]["id"]
    types = (odoo.search_read("mail.activity.type", [["name", "ilike", "call"]], ["id"], limit=1)
             or odoo.search_read("mail.activity.type", [], ["id"], limit=1))
    if not types:
        raise NotFound("No activity type configured in Odoo")
    values = {"res_model_id": model, "res_id": lead_id, "activity_type_id": types[0]["id"],
              "summary": summary, "note": note, "date_deadline": date.isoformat()}
    return odoo.create("mail.activity", values)


def vocabulary(odoo: OdooAPI, limit: int) -> dict:
    """Customer and product names, used only as a speech-recognition hint (not for matching)."""
    customers = odoo.search_read("res.partner", [], ["name"], limit=limit, order="write_date desc")
    products = odoo.search_read("product.product", [["sale_ok", "=", True]], ["name"], limit=limit, order="write_date desc")
    return {"customers": [r["name"] for r in customers], "products": [r["name"] for r in products]}
