"""Items, Quotations and Sales Orders: lookups, validated previews, creation and submission."""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from app.erp.client import ERPAPI, ERPError
from app.services import parties
from app.services.errors import NotFound, ValidationFailed
from app.services.search import fuzzy_find, name_filters, similarity

MAX_QTY = 10_000
MAX_PRICE = 1_000_000
SIMILAR_ITEM_THRESHOLD = 0.85
DEFAULT_UOM, DEFAULT_ITEM_GROUP = "Nos", "Products"
SUMMARY_ROWS = 1000
PRICE_LIST = "Standard Selling"
DOCS = {"Quotation": "Quotation", "Sales Order": "Sales Order"}
_LIST_FIELDS = {
    "Quotation": ["name", "customer_name", "quotation_to", "party_name", "transaction_date", "valid_till", "grand_total",
                  "currency", "status", "docstatus"],
    "Sales Order": ["name", "customer", "customer_name", "transaction_date", "delivery_date", "grand_total", "currency",
                    "status", "docstatus"],
}


def company_currency(erp: ERPAPI) -> str:
    rows = erp.get_list("Company", [], ["default_currency"], 1)
    return rows[0]["default_currency"] if rows else ""


# -- items ---------------------------------------------------------------------------------------
def item_rates(erp: ERPAPI, codes: list[str]) -> dict[str, float]:
    """Selling price per item from ERPNext (Item Price first, then the item's standard rate)."""
    if not codes:
        return {}
    try:
        rates = {r["item_code"]: float(r["price_list_rate"]) for r in erp.get_list(
            "Item Price", [["item_code", "in", codes], ["price_list", "=", PRICE_LIST], ["selling", "=", 1]],
            ["item_code", "price_list_rate"], len(codes) * 3)}
    except ERPError:  # the caller's role cannot read Item Price: fall back to the item's standard rate
        rates = {}
    for r in erp.get_list("Item", [["name", "in", [c for c in codes if c not in rates]]], ["name", "standard_rate"], len(codes)):
        if r.get("standard_rate"):
            rates[r["name"]] = float(r["standard_rate"])
    return rates


def search_items(erp: ERPAPI, q: str, limit: int) -> list[dict]:
    base = [["disabled", "=", 0], ["is_sales_item", "=", 1]]
    fields = ["name", "item_name", "item_group", "standard_rate", "stock_uom"]
    rows = (erp.get_list("Item", base + name_filters("item_name", q), fields, limit, "item_name asc")
            or fuzzy_find(erp, "Item", "item_name", q, fields, filters=base, limit=min(limit, 3)))
    rates = item_rates(erp, [r["name"] for r in rows])
    return [{"id": r["name"], "name": r["item_name"], "list_price": rates.get(r["name"]), "uom": r.get("stock_uom"),
             **{k: r[k] for k in ("match", "score") if k in r}} for r in rows]


# -- creating items (non-stock, with a selling price) ---------------------------------------------------
def _unique_code(erp: ERPAPI, wanted: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Z0-9]+", "-", wanted.upper()).strip("-")[:40] or "ITEM"
    code, n = base, 1
    while code in taken or erp.get_list("Item", [["name", "=", code]], ["name"], 1):
        n += 1
        code = f"{base}-{n}"
    return code


def preview_item(erp: ERPAPI, spec: dict, ignore_ids=frozenset(), taken: set[str] | None = None) -> dict:
    """Validate a new item. Rejects duplicates; asks about look-alikes unless allow_similar is set."""
    name = str(spec.get("name") or "").strip()
    price = spec.get("price")
    if not 2 <= len(name) <= 140:
        raise ValidationFailed("Item name must be 2 to 140 characters")
    if not isinstance(price, (int, float)) or isinstance(price, bool) or not 0 < price <= MAX_PRICE:
        raise ValidationFailed(f"A selling price between 0 and {MAX_PRICE} is required for a new item")
    existing = erp.get_list("Item", [], ["name", "item_name"], 1000)
    for r in existing:
        if (r.get("item_name") or "").lower() == name.lower():
            raise ValidationFailed(f"Item '{r['item_name']}' already exists (id {r['name']}); use it instead of creating it again")
    close = sorted(((similarity(name, r.get("item_name") or ""), r) for r in existing if r["name"] not in ignore_ids),
                   key=lambda t: -t[0])
    if close and close[0][0] >= SIMILAR_ITEM_THRESHOLD and not spec.get("allow_similar"):
        raise ValidationFailed(f"A very similar item exists: '{close[0][1]['item_name']}' (id {close[0][1]['name']}). Ask the user "
                               "whether they mean that one; set allow_similar=true only if they say it is a different item")
    taken = taken if taken is not None else set()
    code = _unique_code(erp, str(spec.get("code") or name), taken)
    taken.add(code)
    return {"code": code, "name": name, "price": float(price), "description": str(spec.get("description") or "")[:1000],
            "uom": str(spec.get("uom") or DEFAULT_UOM)[:40], "item_group": str(spec.get("item_group") or DEFAULT_ITEM_GROUP)[:140]}


def _delete_item(erp: ERPAPI, code: str) -> bool:
    """Best effort removal of an item this app just created (its prices first). If it cannot be deleted, disable it so
    it can never be sold; returns whether it was actually removed."""
    try:
        for row in erp.get_list("Item Price", [["item_code", "=", code]], ["name"], 20):
            erp.delete("Item Price", row["name"])
        erp.delete("Item", code)
        return True
    except ERPError:
        try:
            erp.update("Item", code, {"disabled": 1})
        except ERPError:
            pass
        return False


def create_item(erp: ERPAPI, p: dict) -> dict:
    erp.insert("Item", {"item_code": p["code"], "item_name": p["name"], "item_group": p["item_group"], "stock_uom": p["uom"],
                        "is_stock_item": 0, "is_sales_item": 1, "standard_rate": p["price"], "description": p["description"] or p["name"]})
    try:
        # ERPNext itself creates the "Standard Selling" price from standard_rate; only add it if that did not happen
        if not erp.get_list("Item Price", [["item_code", "=", p["code"]], ["price_list", "=", PRICE_LIST]], ["name"], 1):
            erp.insert("Item Price", {"item_code": p["code"], "price_list": PRICE_LIST, "selling": 1, "price_list_rate": p["price"]})
    except ERPError as exc:
        removed = _delete_item(erp, p["code"])
        raise ERPError(f"{exc} (the new item {p['code']} was {'removed again' if removed else 'created but could not be removed; it is disabled'})")
    return {"id": p["code"], "name": p["name"], "list_price": p["price"], "uom": p["uom"], "item_group": p["item_group"]}


# -- previews and creation ---------------------------------------------------------------------------
def build_preview(erp: ERPAPI, doctype: str, party_type: str, party_id: str, lines: list[dict],
                  delivery_date: str | None = None, valid_till: str | None = None, ignore_ids=frozenset()) -> dict:
    if doctype not in DOCS:
        raise ValidationFailed(f"Unsupported document {doctype}")
    if doctype == "Sales Order" and party_type != "Customer":
        raise ValidationFailed("A Sales Order needs an existing Customer; convert the Lead to a Customer first")
    if party_type not in ("Customer", "Lead"):
        raise ValidationFailed("party_type must be Customer or Lead")
    name = parties.party_name(erp, party_type, party_id)
    if not lines:
        raise ValidationFailed("At least one line is required")
    merged: dict[str, float] = {}
    new_lines: list[tuple[dict, float]] = []
    for line in lines:
        qty = line.get("quantity")
        if not isinstance(qty, (int, float)) or isinstance(qty, bool) or not 0 < qty <= MAX_QTY:
            raise ValidationFailed(f"Each line needs a quantity between 0 and {MAX_QTY}")
        code, spec = str(line.get("item_id") or ""), line.get("new_item")
        if bool(code) == bool(spec):
            raise ValidationFailed("Each line needs exactly one of item_id (existing item) or new_item (item to create)")
        if spec:
            new_lines.append((spec, qty))
        else:
            merged[code] = merged.get(code, 0) + qty
    out_lines, total = [], 0.0
    if merged:
        found = {r["name"]: r for r in erp.get_list("Item", [["name", "in", list(merged)], ["disabled", "=", 0], ["is_sales_item", "=", 1]],
                                                    ["name", "item_name"], len(merged))}
        missing = [c for c in merged if c not in found]
        if missing:
            raise NotFound(f"Item {missing[0]} not found or not sellable")
        rates = item_rates(erp, list(merged))
        for code, qty in merged.items():
            if code not in rates:
                raise ValidationFailed(f"Item {found[code]['item_name']} has no selling price in ERPNext")
            amount = round(qty * rates[code], 3)
            total += amount
            out_lines.append({"item_id": code, "item_name": found[code]["item_name"], "quantity": qty,
                              "rate": rates[code], "amount": amount})
    taken: set[str] = set()
    names: set[str] = set()
    for spec, qty in new_lines:
        item = preview_item(erp, spec, ignore_ids, taken)
        if item["name"].lower() in names:
            raise ValidationFailed(f"New item '{item['name']}' appears twice; combine the quantities into one line")
        names.add(item["name"].lower())
        amount = round(qty * item["price"], 3)
        total += amount
        out_lines.append({"item_id": item["code"], "item_name": item["name"], "quantity": qty, "rate": item["price"],
                          "amount": amount, "new_item": item})
    today = dt.date.today()
    preview = {"doctype": doctype, "party_type": party_type, "party_id": party_id, "party_name": name,
               "lines": out_lines, "total_before_tax": round(total, 3), "currency": company_currency(erp)}
    if doctype == "Sales Order":
        preview["delivery_date"] = _future_date(delivery_date, today + dt.timedelta(days=14))
    else:
        preview["valid_till"] = _future_date(valid_till, today + dt.timedelta(days=30))
    return preview


def _future_date(value: str | None, default: dt.date) -> str:
    if not value:
        return default.isoformat()
    try:
        d = dt.date.fromisoformat(value)
    except ValueError:
        raise ValidationFailed("dates must be YYYY-MM-DD")
    if d < dt.date.today():
        raise ValidationFailed("The date cannot be in the past")
    return d.isoformat()


def create_document(erp: ERPAPI, p: dict) -> dict:
    today = dt.date.today().isoformat()
    created: list[str] = []
    try:
        for line in p["lines"]:
            if line.get("new_item"):
                created.append(create_item(erp, line["new_item"])["id"])
        items = [{"item_code": l["item_id"], "qty": l["quantity"], "rate": l["rate"],
                  **({"delivery_date": p["delivery_date"]} if p["doctype"] == "Sales Order" else {})} for l in p["lines"]]
        if p["doctype"] == "Quotation":
            doc = {"quotation_to": p["party_type"], "party_name": p["party_id"], "transaction_date": today,
                   "valid_till": p["valid_till"], "order_type": "Sales", "items": items}
        else:
            doc = {"customer": p["party_id"], "transaction_date": today, "delivery_date": p["delivery_date"],
                   "order_type": "Sales", "items": items}
        saved = erp.insert(p["doctype"], doc)
    except ERPError as exc:
        left = [c for c in created if not _delete_item(erp, c)]
        note = (f" The new item(s) {', '.join(left)} were created before this failed and could not be removed (they are disabled)." if left else
                (" The new item(s) were removed again." if created else ""))
        raise ERPError(f"{exc}{note}")
    out = _summary(p["doctype"], saved)
    out["created_items"] = created
    return out


def quotation_to_order(erp: ERPAPI, quotation_id: str, delivery_date: str | None = None) -> dict:
    """Create a draft Sales Order from a submitted Quotation using ERPNext's own mapping."""
    q = _get(erp, "Quotation", quotation_id)
    if q["docstatus"] != 1:
        raise ValidationFailed("The quotation must be submitted before it can become a Sales Order")
    mapped = erp.call("erpnext.selling.doctype.quotation.quotation.make_sales_order", source_name=quotation_id)
    mapped["delivery_date"] = _future_date(delivery_date, dt.date.today() + dt.timedelta(days=14))
    for row in mapped.get("items", []):
        row["delivery_date"] = mapped["delivery_date"]
    return _summary("Sales Order", erp.insert("Sales Order", mapped))


def preview_submit(erp: ERPAPI, doctype: str, doc_id: str) -> dict:
    if doctype not in DOCS:
        raise ValidationFailed(f"Unsupported document {doctype}")
    d = _get(erp, doctype, doc_id)
    if d["docstatus"] != 0:
        raise ValidationFailed(f"{doc_id} is not a draft (status {d.get('status')})")
    return {"doctype": doctype, "id": doc_id, "party_name": d.get("customer_name"), "grand_total": d.get("grand_total"),
            "currency": d.get("currency")}


def submit(erp: ERPAPI, doctype: str, doc_id: str) -> dict:
    return _summary(doctype, erp.submit(doctype, doc_id))


def _summary(doctype: str, doc: dict) -> dict[str, Any]:
    return {"doctype": doctype, "id": doc["name"], "party_name": doc.get("customer_name"),
            "grand_total": doc.get("grand_total"), "currency": doc.get("currency"), "status": doc.get("status")}


def _get(erp: ERPAPI, doctype: str, name: str) -> dict:
    rows = erp.get_list(doctype, [["name", "=", name]], _LIST_FIELDS[doctype] + ["docstatus"], 1)
    if not rows:
        raise NotFound(f"{doctype} {name} not found")
    return rows[0]


# -- reads ---------------------------------------------------------------------------------------------
def _filters(party_field: str, customer: str | None, status: str | None, date_from, date_to, finalized: bool) -> list:
    filters: list = name_filters(party_field, customer) if customer else []
    if status:
        filters.append(["status", "=", status])
    if finalized:
        filters.append(["docstatus", "=", 1])
    else:
        filters.append(["docstatus", "<", 2])
    if date_from:
        filters.append(["transaction_date", ">=", date_from.isoformat()])
    if date_to:
        filters.append(["transaction_date", "<=", date_to.isoformat()])
    return filters


def list_documents(erp: ERPAPI, doctype: str, limit: int, customer: str | None = None, status: str | None = None,
                   date_from: dt.date | None = None, date_to: dt.date | None = None, finalized: bool = False) -> list[dict]:
    if doctype not in DOCS:
        raise ValidationFailed(f"Unsupported document {doctype}")
    if date_from and date_to and date_from > date_to:
        raise ValidationFailed("date_from is after date_to")
    return erp.get_list(doctype, _filters("customer_name", customer, status, date_from, date_to, finalized),
                        _LIST_FIELDS[doctype], limit, "transaction_date desc, modified desc")


def get_document(erp: ERPAPI, doctype: str, doc_id: str) -> dict:
    if doctype not in DOCS and doctype not in ("Opportunity", "Lead"):
        raise ValidationFailed(f"Unsupported document {doctype}")
    doc = erp.get_doc(doctype, doc_id)
    keep = ("name", "customer_name", "party_name", "status", "docstatus", "transaction_date", "valid_till", "delivery_date",
            "grand_total", "total", "total_taxes_and_charges", "currency", "owner", "sales_stage", "opportunity_amount",
            "expected_closing", "company_name", "lead_name", "source")
    out = {k: doc[k] for k in keep if doc.get(k) not in (None, "")}
    if doc.get("items"):
        out["items"] = [{k: row.get(k) for k in ("item_code", "item_name", "qty", "rate", "amount")} for row in doc["items"]]
    return out


def summary(erp: ERPAPI, doctype: str, date_from: dt.date | None, date_to: dt.date | None, customer: str | None = None) -> dict:
    """Count and total value per status, for 'how many / how much' questions."""
    amount, date_field = {"Quotation": ("grand_total", "transaction_date"), "Sales Order": ("grand_total", "transaction_date"),
                          "Opportunity": ("opportunity_amount", "transaction_date")}.get(doctype, (None, None))
    if not amount:
        raise ValidationFailed("summary supports Quotation, Sales Order and Opportunity")
    filters: list = [["docstatus", "<", 2]]
    if customer:
        filters += name_filters("customer_name", customer)
    if date_from:
        filters.append([date_field, ">=", date_from.isoformat()])
    if date_to:
        filters.append([date_field, "<=", date_to.isoformat()])
    rows = erp.get_list(doctype, filters, ["name", "status", amount], SUMMARY_ROWS)  # aggregated here: bounded and portable
    by_status: dict[str, dict] = {}
    for r in rows:
        b = by_status.setdefault(r.get("status") or "Unknown", {"status": r.get("status") or "Unknown", "count": 0, "total": 0.0})
        b["count"] += 1
        b["total"] += float(r.get(amount) or 0)
    groups = [{**b, "total": round(b["total"], 3)} for b in by_status.values()]
    return {"doctype": doctype, "by_status": groups, "count": len(rows), "total": round(sum(g["total"] for g in groups), 3),
            "currency": company_currency(erp), "truncated": len(rows) >= SUMMARY_ROWS}
