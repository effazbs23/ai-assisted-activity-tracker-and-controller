"""In-memory Odoo stand-in with a tiny domain matcher (=, in, ilike, <, >, <=, >=)."""
from __future__ import annotations


def _match(row, cond):
    field, op, val = cond
    cur = row
    for part in field.split("."):
        if isinstance(cur, list) and part == "name":  # many2one as [id, display_name]
            cur = cur[1]
        else:
            cur = cur.get(part) if isinstance(cur, dict) else None
    if field == "partner_ids" and op == "in":
        return bool(set(row["partner_ids"]) & set(val))
    if op == "=": return cur == val
    if op == "in": return cur in val
    if op == "=ilike": return cur is not None and str(val).lower() == str(cur).lower()
    if op == "ilike": return cur is not None and str(val).lower() in str(cur).lower()
    if op == "<": return cur < val
    if op == ">": return cur > val
    if op == "<=": return cur <= val
    if op == ">=": return cur >= val
    raise NotImplementedError(op)


class FakeOdoo:
    def __init__(self):
        self.uid = None
        self.next_id = 100
        self.created, self.called = [], []
        self.t = {
            "res.partner": [{"id": 3, "name": "Administrator"}, {"id": 6, "name": "XYZ Traders", "email": "a@b", "phone": "1"},
                            {"id": 7, "name": "XYZ Interiors", "email": "", "phone": ""}],
            "res.users": [{"id": 2, "partner_id": [3, "Administrator"]}],
            "product.product": [{"id": 11, "name": "Workstation", "list_price": 500.0, "default_code": "WS", "sale_ok": True},
                                {"id": 12, "name": "Hidden", "list_price": 1.0, "default_code": "", "sale_ok": False}],
            "crm.lead": [{"id": 1, "name": "XYZ Traders - Office Furniture Supply", "partner_id": [6, "XYZ Traders"]}],
            "ir.model": [{"id": 99, "model": "crm.lead"}],
            "mail.activity.type": [{"id": 2, "name": "Call"}],
            "res.company": [{"id": 1, "currency_id": [2, "USD"]}],
            "sale.order": [], "calendar.event": [],
        }

    def authenticate(self):
        self.uid = 2
        return 2

    def search_read(self, model, domain, fields, limit=10, order=None):
        rows = [r for r in self.t.get(model, []) if all(_match(r, c) for c in domain)]
        return [dict(r) for r in rows[:limit]]

    def create(self, model, values):
        self.created.append((model, values))
        self.next_id += 1
        row = {"id": self.next_id, **{k: v for k, v in values.items() if k != "order_line"}}
        if model == "sale.order":
            lines = values["order_line"]
            price = {p["id"]: p["list_price"] for p in self.t["product.product"]}
            partner = next(p for p in self.t["res.partner"] if p["id"] == values["partner_id"])
            row.update(name=f"S{self.next_id:05d}", state="draft", partner_id=[partner["id"], partner["name"]],
                       amount_total=sum(l[2]["product_uom_qty"] * price[l[2]["product_id"]] for l in lines),
                       date_order="2026-10-14 10:00:00", order_lines=lines)
        if model == "product.product":
            row.setdefault("sale_ok", True)
            row.setdefault("default_code", "")
        if model == "calendar.event":
            row["partner_ids"] = values["partner_ids"][0][2]
        self.t.setdefault(model, []).append(row)
        return self.next_id

    def write(self, model, ids, values):
        self.written = getattr(self, "written", []) + [(model, ids, values)]
        for r in self.t[model]:
            if r["id"] in ids:
                r.update(values)
        return True

    def call(self, model, method, ids, **kw):
        self.called.append((model, method, ids))
        if method == "action_confirm":
            for r in self.t[model]:
                if r["id"] == ids[0]:
                    r["state"] = "sale"
