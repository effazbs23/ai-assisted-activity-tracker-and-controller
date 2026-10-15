from __future__ import annotations

import datetime as dt
from typing import Any

from app.odoo.client import OdooAPI
from app.services.errors import NotFound, ValidationFailed
from app.services.search import SIMILAR_PRODUCT_THRESHOLD, fuzzy_find, name_domain

MAX_QTY = 10_000


def search_products(odoo: OdooAPI, q: str, limit: int) -> list[dict]:
    fields = ["id", "name", "list_price", "default_code"]
    rows = odoo.search_read("product.product", [*name_domain("name", q), ["sale_ok", "=", True]], fields,
                            limit=limit, order="name")
    return rows or fuzzy_find(odoo, "product.product", "name", q, fields, [["sale_ok", "=", True]], limit=min(limit, 3))


MAX_PRICE = 1_000_000
STATES = {"draft", "sent", "sale", "cancel"}


def _clean_new_product(np: dict) -> dict:
    name = (np.get("name") or "").strip()
    price = np.get("price")
    if not 2 <= len(name) <= 120:
        raise ValidationFailed("Product name must be 2..120 characters")
    if price is None or not 0 < float(price) <= MAX_PRICE:
        raise ValidationFailed(f"A price between 0 and {MAX_PRICE} is required to create product '{name}'")
    return {"name": name, "price": round(float(price), 2),
            "description": (np.get("description") or "").strip()[:1000],
            "default_code": (np.get("default_code") or "").strip()[:40],
            "allow_similar": bool(np.get("allow_similar"))}


def check_product_free(odoo: OdooAPI, name: str) -> None:
    dup = odoo.search_read("product.product", [["name", "=ilike", name]], ["id", "name", "list_price"], limit=1)
    if dup:
        raise ValidationFailed(f"Product '{dup[0]['name']}' already exists (id {dup[0]['id']}, price {dup[0]['list_price']}); use it instead")


def preview_product(odoo: OdooAPI, np: dict) -> dict:
    clean = _clean_new_product(np)
    check_product_free(odoo, clean["name"])
    if not clean["allow_similar"]:
        close = fuzzy_find(odoo, "product.product", "name", clean["name"], ["list_price"],
                           [["sale_ok", "=", True]], limit=1, threshold=SIMILAR_PRODUCT_THRESHOLD)
        if close:
            raise ValidationFailed(
                f"A very similar product exists: '{close[0]['name']}' (id {close[0]['id']}, price {close[0]['list_price']}). "
                "Ask the user whether they meant that one; only if they say it is a different product, retry with allow_similar=true")
    return clean


def create_product(odoo: OdooAPI, clean: dict) -> dict:
    check_product_free(odoo, clean["name"])
    values = {"name": clean["name"], "list_price": clean["price"], "sale_ok": True}
    if clean.get("description"):
        values["description_sale"] = clean["description"]
    if clean.get("default_code"):
        values["default_code"] = clean["default_code"]
    pid = odoo.create("product.product", values)
    return odoo.search_read("product.product", [["id", "=", pid]], ["id", "name", "list_price", "default_code"], limit=1)[0]


def build_preview(odoo: OdooAPI, partner_id: int, lines: list[dict[str, Any]]) -> dict:
    """Validate a draft order against Odoo. Existing products are priced by Odoo; only a *new* product takes the user's price."""
    if not lines:
        raise ValidationFailed("An order needs at least one line")
    partner = odoo.search_read("res.partner", [["id", "=", partner_id]], ["id", "name"], limit=1)
    if not partner:
        raise NotFound(f"Customer {partner_id} not found")
    out, new_names = [], set()
    for line in lines:
        qty = line["quantity"]
        if not 0 < qty <= MAX_QTY:
            raise ValidationFailed(f"Quantity {qty} is out of range (1..{MAX_QTY})")
        if line.get("new_product"):
            if line.get("product_id"):
                raise ValidationFailed("A line has either product_id or new_product, not both")
            np = preview_product(odoo, line["new_product"])
            if np["name"].lower() in new_names:
                raise ValidationFailed(f"Product '{np['name']}' appears twice as new")
            new_names.add(np["name"].lower())
            out.append({"product_id": None, "name": np["name"], "quantity": qty, "list_price": np["price"],
                        "new_product": np})
            continue
        prod = odoo.search_read("product.product", [["id", "=", line.get("product_id")], ["sale_ok", "=", True]],
                                ["id", "name", "list_price"], limit=1)
        if not prod:
            raise NotFound(f"Product {line.get('product_id')} not found or not sellable")
        out.append({"product_id": prod[0]["id"], "name": prod[0]["name"], "quantity": qty,
                    "list_price": prod[0]["list_price"]})
    return {"partner_id": partner[0]["id"], "partner_name": partner[0]["name"], "lines": out,
            "new_products": [l["name"] for l in out if l.get("new_product")],
            "subtotal_excl_tax": round(sum(l["quantity"] * l["list_price"] for l in out), 2)}


def create_draft_order(odoo: OdooAPI, preview: dict) -> dict:
    created: list[int] = []
    try:
        lines = []
        for l in preview["lines"]:
            pid = l["product_id"]
            if l.get("new_product"):
                pid = create_product(odoo, l["new_product"])["id"]
                created.append(pid)
            lines.append((0, 0, {"product_id": pid, "product_uom_qty": l["quantity"]}))
        order_id = odoo.create("sale.order", {"partner_id": preview["partner_id"], "order_line": lines})
    except Exception:
        for pid in created:  # no delete right by design: archive products created for this failed order
            try:
                odoo.write("product.product", [pid], {"active": False})
            except Exception:
                pass
        raise
    row = odoo.search_read("sale.order", [["id", "=", order_id]], ["id", "name", "state", "amount_total"], limit=1)[0]
    row["created_product_ids"] = created
    return row


def confirm_order(odoo: OdooAPI, order_id: int) -> dict:
    rows = odoo.search_read("sale.order", [["id", "=", order_id]], ["id", "state"], limit=1)
    if not rows:
        raise NotFound(f"Order {order_id} not found")
    if rows[0]["state"] not in ("draft", "sent"):
        raise ValidationFailed(f"Order is already {rows[0]['state']}")
    odoo.call("sale.order", "action_confirm", [order_id])
    return odoo.search_read("sale.order", [["id", "=", order_id]], ["id", "name", "state", "amount_total"], limit=1)[0]


def list_quotations(odoo: OdooAPI, limit: int, customer: str | None = None,
                    date_from: dt.date | None = None, date_to: dt.date | None = None) -> list[dict]:
    domain: list = [["state", "in", ["draft", "sent"]]]
    if customer:
        domain.extend(name_domain("partner_id.name", customer))
    if date_from:
        domain.append(["date_order", ">=", f"{date_from} 00:00:00"])
    if date_to:
        domain.append(["date_order", "<=", f"{date_to} 23:59:59"])
    return odoo.search_read("sale.order", domain, ["id", "name", "partner_id", "date_order", "amount_total", "state"],
                            limit=limit, order="date_order desc")


def list_orders(odoo: OdooAPI, limit: int, customer: str | None = None, state: str | None = None,
                date_from: dt.date | None = None, date_to: dt.date | None = None) -> list[dict]:
    """All sales documents (quotations and confirmed orders), newest first."""
    if state and state not in STATES:
        raise ValidationFailed(f"state must be one of {sorted(STATES)}")
    domain: list = []
    if state:
        domain.append(["state", "=", state])
    if customer:
        domain.extend(name_domain("partner_id.name", customer))
    if date_from:
        domain.append(["date_order", ">=", f"{date_from} 00:00:00"])
    if date_to:
        domain.append(["date_order", "<=", f"{date_to} 23:59:59"])
    return odoo.search_read("sale.order", domain, ["id", "name", "partner_id", "date_order", "amount_total", "state"],
                            limit=limit, order="date_order desc")


def get_order(odoo: OdooAPI, order_id: int) -> dict:
    rows = odoo.search_read("sale.order", [["id", "=", order_id]],
                            ["id", "name", "partner_id", "date_order", "amount_untaxed", "amount_total", "state"], limit=1)
    if not rows:
        raise NotFound(f"Order {order_id} not found")
    lines = odoo.search_read("sale.order.line", [["order_id", "=", order_id]],
                             ["name", "product_uom_qty", "price_unit", "price_subtotal"], limit=100)
    return {**rows[0], "lines": lines}
