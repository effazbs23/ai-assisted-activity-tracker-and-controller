"""Customers, Leads and Opportunities: name search (exact, then fuzzy) and lookups."""
from __future__ import annotations

from typing import Any

from app.erp.client import ERPAPI
from app.services.errors import NotFound
from app.services.search import fuzzy_find, name_filters

PARTY_TYPES = ("Customer", "Lead", "Opportunity")
_NAME_FIELD = {"Customer": "customer_name", "Lead": "company_name", "Opportunity": "customer_name"}


def _norm(row: dict, display: str, extra: tuple[str, ...] = ()) -> dict[str, Any]:
    out = {"id": row["name"], "name": row.get(display) or row.get("lead_name") or row["name"]}
    out.update({k: row.get(k) for k in extra})
    out.update({k: row[k] for k in ("match", "score") if k in row})
    return out


def search_customers(erp: ERPAPI, q: str, limit: int) -> list[dict]:
    fields = ["name", "customer_name", "customer_group", "territory"]
    rows = (erp.get_list("Customer", name_filters("customer_name", q), fields, limit, "customer_name asc")
            or fuzzy_find(erp, "Customer", "customer_name", q, fields, limit=min(limit, 3)))
    return [_norm(r, "customer_name", ("customer_group", "territory")) for r in rows]


def search_leads(erp: ERPAPI, q: str | None, limit: int) -> list[dict]:
    """Match the organization name, then the contact person; empty query returns the latest leads."""
    fields = ["name", "lead_name", "company_name", "status", "lead_owner", "source"]
    extra = ("status", "lead_owner")
    if not q:
        rows = erp.get_list("Lead", [], fields, limit, "modified desc")
    else:
        rows = (erp.get_list("Lead", name_filters("company_name", q), fields, limit, "modified desc")
                or erp.get_list("Lead", name_filters("lead_name", q), fields, limit, "modified desc")
                or fuzzy_find(erp, "Lead", "company_name", q, fields, limit=min(limit, 3)))
    return [_norm(r, "company_name", extra) for r in rows]


def search_opportunities(erp: ERPAPI, q: str | None, limit: int, status: str | None = None) -> list[dict]:
    fields = ["name", "customer_name", "party_name", "opportunity_from", "status", "sales_stage",
              "opportunity_amount", "expected_closing", "currency"]
    extra = ("opportunity_from", "status", "sales_stage", "opportunity_amount", "expected_closing")
    base = [["status", "=", status]] if status else []
    if not q:
        rows = erp.get_list("Opportunity", base, fields, limit, "modified desc")
    else:
        rows = (erp.get_list("Opportunity", base + name_filters("customer_name", q), fields, limit, "modified desc")
                or fuzzy_find(erp, "Opportunity", "customer_name", q, fields, filters=base, limit=min(limit, 3)))
    return [_norm(r, "customer_name", extra) for r in rows]


def search_clients(erp: ERPAPI, q: str, limit: int) -> list[dict]:
    """Customers and Leads together, each row tagged with its type. Exact matches first; otherwise the closest names."""
    def tag(rows, kind):
        return [{**r, "type": kind} for r in rows]
    exact = (tag(_exact(erp, "Customer", q, limit), "Customer") + tag(_exact_leads(erp, q, limit), "Lead"))[:limit]
    if exact:
        return exact
    near = (tag(search_customers(erp, q, 3), "Customer") + tag(search_leads(erp, q, 3), "Lead"))
    return sorted(near, key=lambda r: -r.get("score", 0))[:3]


def _exact(erp: ERPAPI, doctype: str, q: str, limit: int) -> list[dict]:
    rows = erp.get_list(doctype, name_filters("customer_name", q), ["name", "customer_name", "customer_group", "territory"],
                        limit, "customer_name asc")
    return [_norm(r, "customer_name", ("customer_group", "territory")) for r in rows]


def _exact_leads(erp: ERPAPI, q: str, limit: int) -> list[dict]:
    fields = ["name", "lead_name", "company_name", "status", "lead_owner", "source"]
    rows = (erp.get_list("Lead", name_filters("company_name", q), fields, limit, "modified desc")
            or erp.get_list("Lead", name_filters("lead_name", q), fields, limit, "modified desc"))
    return [_norm(r, "company_name", ("status", "lead_owner")) for r in rows]


def party_name(erp: ERPAPI, party_type: str, party_id: str) -> str:
    """Display name of a Customer/Lead/Opportunity; raises NotFound if it does not exist."""
    if party_type not in PARTY_TYPES:
        raise NotFound(f"Unknown party type {party_type}")
    field = _NAME_FIELD[party_type]
    rows = erp.get_list(party_type, [["name", "=", party_id]], ["name", field, "lead_name"] if party_type == "Lead" else ["name", field], 1)
    if not rows:
        raise NotFound(f"{party_type} {party_id} not found")
    return rows[0].get(field) or rows[0].get("lead_name") or rows[0]["name"]


def vocabulary(erp: ERPAPI, limit: int) -> dict:
    """Customer and item names, used only as a speech-recognition hint (not for matching)."""
    customers = erp.get_list("Customer", [], ["customer_name"], limit, "modified desc")
    leads = erp.get_list("Lead", [], ["company_name"], limit, "modified desc")
    items = erp.get_list("Item", [["is_sales_item", "=", 1], ["disabled", "=", 0]], ["item_name"], limit, "modified desc")
    return {"customers": [r["customer_name"] for r in customers] + [r["company_name"] for r in leads if r.get("company_name")],
            "products": [r["item_name"] for r in items]}
