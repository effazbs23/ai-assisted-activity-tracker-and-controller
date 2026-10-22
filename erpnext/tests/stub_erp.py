"""In-memory stand-in for ERPNext's REST API, used by the offline tests.

It implements the same interface as app.erp.client.ERPClient (get_list, get_doc, insert, update, submit, call)
with the filter operators the services use, so tests run without a server.
"""
from __future__ import annotations

import copy
import datetime as dt
import itertools

from app.erp.client import ERPError

TODAY = dt.date.today()


def iso(days_ago: int) -> str:
    return (TODAY - dt.timedelta(days=days_ago)).isoformat()


def _match(row: dict, flt: list) -> bool:
    field, op, value = flt
    have = row.get(field)
    if op == "=":
        return have == value
    if op == "!=":
        return have != value
    if op == "like":
        return str(value).strip("%").lower() in str(have or "").lower()
    if op == "in":
        return have in value
    if op in (">=", "<=", "<", ">"):
        if have is None:
            return False
        return {">=": have >= value, "<=": have <= value, "<": have < value, ">": have > value}[op]
    raise AssertionError(f"stub does not support operator {op}")


class StubERPNext:
    def __init__(self, user: str = "alice@example.com", deny: set[str] | None = None):
        self.user = user
        self.deny = deny or set()           # doctypes this user may not read
        self.deny_insert: set[str] = set()  # doctypes this user may not create
        self.auto_item_price = True         # like ERPNext: an Item with standard_rate gets a Standard Selling Item Price
        self.deny_delete: set[str] = set()  # doctypes this user may not delete
        self.unreachable = False
        self.calls: list[tuple] = []
        self._seq = itertools.count(1)
        self.db: dict[str, list[dict]] = {
            "Company": [{"name": "Demo Co", "default_currency": "OMR"}],
            "Customer": [{"name": n, "customer_name": n, "customer_group": "Commercial", "territory": "Oman"}
                         for n in ("Nizwa Builders", "Gulf Interiors LLC", "XYZ Traders")],
            "Lead": [{"name": "CRM-LEAD-0001", "lead_name": "XYZ Interiors", "company_name": "XYZ Interiors", "status": "Open",
                      "lead_owner": None, "source": "Reference"}],
            "Item": [{"name": "WS-4", "item_name": "Workstation 4-Seat", "item_group": "Products", "standard_rate": 350,
                      "stock_uom": "Nos", "disabled": 0, "is_sales_item": 1},
                     {"name": "CHAIR-ERG", "item_name": "Ergonomic Chair", "item_group": "Products", "standard_rate": 95,
                      "stock_uom": "Nos", "disabled": 0, "is_sales_item": 1},
                     {"name": "OLD-1", "item_name": "Retired Table", "item_group": "Products", "standard_rate": 10,
                      "stock_uom": "Nos", "disabled": 1, "is_sales_item": 1},
                     {"name": "NOPRICE", "item_name": "Priceless Thing", "item_group": "Products", "standard_rate": 0,
                      "stock_uom": "Nos", "disabled": 0, "is_sales_item": 1}],
            "Item Price": [{"name": "IP1", "item_code": "WS-4", "price_list": "Standard Selling", "selling": 1, "price_list_rate": 360}],
            "Sales Stage": [{"name": n} for n in ("Prospecting", "Qualification", "Proposal/Price Quote", "Negotiation/Review")],
            "Opportunity": [{"name": "CRM-OPP-0001", "customer_name": "Gulf Interiors LLC", "party_name": "Gulf Interiors LLC",
                             "opportunity_from": "Customer", "status": "Open", "sales_stage": "Qualification",
                             "opportunity_amount": 8500, "transaction_date": iso(10), "docstatus": 0, "currency": "OMR"}],
            "Quotation": [
                {"name": "SAL-QTN-0001", "customer_name": "Nizwa Builders", "quotation_to": "Customer", "party_name": "Nizwa Builders",
                 "transaction_date": iso(40), "valid_till": iso(-10), "grand_total": 1000.0, "currency": "OMR", "status": "Open",
                 "docstatus": 1, "items": [{"item_code": "WS-4", "item_name": "Workstation 4-Seat", "qty": 2, "rate": 360, "amount": 720}]},
                {"name": "SAL-QTN-0002", "customer_name": "XYZ Traders", "quotation_to": "Customer", "party_name": "XYZ Traders",
                 "transaction_date": iso(2), "valid_till": iso(-20), "grand_total": 500.0, "currency": "OMR", "status": "Draft",
                 "docstatus": 0, "items": []},
                {"name": "SAL-QTN-0003", "customer_name": "Gulf Interiors LLC", "quotation_to": "Customer", "party_name": "Gulf Interiors LLC",
                 "transaction_date": iso(3), "valid_till": iso(-20), "grand_total": 250.0, "currency": "OMR", "status": "Open",
                 "docstatus": 1, "items": []}],
            "Sales Order": [{"name": "SAL-ORD-0001", "customer": "Nizwa Builders", "customer_name": "Nizwa Builders",
                             "transaction_date": iso(1), "delivery_date": iso(-14), "grand_total": 1000.0, "currency": "OMR",
                             "status": "To Deliver and Bill", "docstatus": 1, "items": []}],
            "Sales Activity": [
                {"name": "SACT-00001", "client_name": "Nizwa Builders", "activity_type": "Meeting", "activity_date": iso(3),
                 "status": "Completed", "details": "Site visit", "owner": "alice@example.com"},
                {"name": "SACT-00002", "client_name": "XYZ Traders", "activity_type": "Quotation Sent", "activity_date": iso(1),
                 "status": "Awaiting Client", "details": "20 workstations", "owner": "bob@example.com"}],
        }
        self.created: list[tuple[str, dict]] = []
        self.deleted: list[tuple[str, str]] = []

    # -- ERPClient interface ------------------------------------------------------------------------
    def _guard(self, doctype: str):
        if self.unreachable:
            raise ERPError("ERPNext unreachable: connection refused")
        if doctype in self.deny:
            raise ERPError(f"ERPNext denied access: User {self.user} does not have doctype access via role permission for document {doctype}")

    def whoami(self) -> str:
        return self.user

    def get_list(self, doctype, filters=None, fields=None, limit=10, order_by=None, or_filters=None):
        self._guard(doctype)
        rows = [r for r in self.db.get(doctype, []) if all(_match(r, f) for f in (filters or []))]
        if or_filters:
            rows = [r for r in rows if any(_match(r, f) for f in or_filters)]
        if order_by:
            col, _, direction = order_by.split(",")[0].strip().partition(" ")
            rows.sort(key=lambda r: str(r.get(col) or ""), reverse=direction.lower() == "desc")
        fields = [f for f in (fields or ["name"])]
        return [{f: copy.deepcopy(r.get(f)) for f in fields if " as " not in f} for r in rows[:limit]]

    def get_doc(self, doctype, name):
        self._guard(doctype)
        for r in self.db.get(doctype, []):
            if r["name"] == name:
                return copy.deepcopy(r)
        raise ERPError("ERPNext record or method not found")

    def insert(self, doctype, doc):
        self._guard(doctype)
        if doctype in self.deny_insert:
            raise ERPError(f"ERPNext denied access: User {self.user} does not have permission to create {doctype}")
        doc = copy.deepcopy(doc)
        if doctype == "Item":
            doc.update(name=doc["item_code"], disabled=0)
            self.db["Item"].append(doc)
            if self.auto_item_price and doc.get("standard_rate"):   # ERPNext creates the Standard Selling price itself
                self.db["Item Price"].append({"name": f"IP-AUTO{next(self._seq):04d}", "item_code": doc["item_code"],
                                              "price_list": "Standard Selling", "selling": 1, "price_list_rate": doc["standard_rate"]})
            self.created.append((doctype, copy.deepcopy(doc)))
            return copy.deepcopy(doc)
        prefix = {"Quotation": "SAL-QTN", "Sales Order": "SAL-ORD", "Lead": "CRM-LEAD", "Opportunity": "CRM-OPP",
                  "Sales Activity": "SACT"}.get(doctype, doctype.upper().replace(" ", "-"))
        doc["name"] = f"{prefix}-NEW{next(self._seq):04d}"
        if doctype == "Item Price":
            doc["name"] = f"IP-NEW{next(self._seq):04d}"
        doc.setdefault("docstatus", 0)
        doc["owner"] = self.user
        if doctype in ("Quotation", "Sales Order"):
            doc["status"] = "Draft"
            doc["currency"] = "OMR"
            doc["grand_total"] = round(sum(i["qty"] * i["rate"] for i in doc["items"]) * 1.05, 3)  # a 5% tax
            party = doc.get("party_name") or doc.get("customer")
            doc["customer_name"] = party if doctype == "Sales Order" or doc.get("quotation_to") == "Customer" else party
        if doctype == "Opportunity":
            doc["customer_name"] = doc["party_name"]
            doc.setdefault("sales_stage", "Prospecting")
        self.db.setdefault(doctype, []).append(doc)
        self.created.append((doctype, copy.deepcopy(doc)))
        return copy.deepcopy(doc)

    def delete(self, doctype, name):
        self._guard(doctype)
        if doctype in self.deny_delete:
            raise ERPError(f"ERPNext denied access: User {self.user} does not have permission to delete {doctype}")
        rows = self.db.get(doctype, [])
        if not any(r["name"] == name for r in rows):
            raise ERPError("ERPNext record or method not found")
        self.db[doctype] = [r for r in rows if r["name"] != name]
        self.deleted.append((doctype, name))

    def update(self, doctype, name, values):
        self._guard(doctype)
        for r in self.db[doctype]:
            if r["name"] == name:
                r.update(values)
                return copy.deepcopy(r)
        raise ERPError("ERPNext record or method not found")

    def submit(self, doctype, name):
        self._guard(doctype)
        for r in self.db[doctype]:
            if r["name"] == name:
                if r["docstatus"] != 0:
                    raise ERPError("Document is already submitted")
                r["docstatus"] = 1
                r["status"] = "Open" if doctype == "Quotation" else "To Deliver and Bill"
                return copy.deepcopy(r)
        raise ERPError("ERPNext record or method not found")

    def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if method.endswith("quotation.make_sales_order"):
            q = self.get_doc("Quotation", kwargs["source_name"])
            return {"customer": q["party_name"], "customer_name": q["customer_name"], "transaction_date": TODAY.isoformat(),
                    "items": [{"item_code": i["item_code"], "qty": i["qty"], "rate": i["rate"]} for i in q["items"]] or
                             [{"item_code": "WS-4", "qty": 1, "rate": 360}]}
        if method.endswith("get_count"):
            return len([r for r in self.db.get(kwargs["doctype"], []) if all(_match(r, f) for f in kwargs.get("filters") or [])])
        if method.endswith("add_comment"):
            return {"name": "COMMENT-1"}
        raise ERPError(f"stub: unknown method {method}")


def by_caller(**stubs: StubERPNext):
    """erp_for callable for create_app: caller name -> its own stub (own ERPNext user)."""
    def erp_for(caller: str):
        if caller not in stubs:
            raise ERPError(f"No ERPNext user is configured for caller '{caller}'")
        return stubs[caller]
    return erp_for
