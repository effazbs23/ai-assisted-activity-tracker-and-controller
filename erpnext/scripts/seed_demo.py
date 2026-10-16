"""Demo data for the ERPNext assistant. Run scripts/setup_erpnext.py first.

Master data (customers, items, prices, leads) is created as Administrator. Opportunities, quotations,
sales orders and activities are created as the sales users themselves, so ownership and permissions look real.
Idempotent: master data is created only if missing; transactional data only into empty areas.

    venv/bin/python scripts/seed_demo.py
"""
from __future__ import annotations

import datetime as dt
import os

from _common import ERP_URL, AdminSession  # noqa: E402  (also sets sys.path)

from app.config import Settings
from app.erp.client import ERPClient, ERPError

TODAY = dt.date.today()
D = lambda days: (TODAY - dt.timedelta(days=days)).isoformat()

CUSTOMERS = ["Nizwa Builders", "Gulf Interiors LLC", "XYZ Traders", "Muscat Office Solutions", "Sohar Contracting",
             "Salalah Hospitality Group"]
ITEMS = [  # code, name, price (OMR)
    ("WS-4", "Workstation 4-Seat", 350), ("DESK-EX", "Executive Desk", 420), ("CHAIR-ERG", "Ergonomic Chair", 95),
    ("TBL-CONF", "Conference Table", 780), ("CAB-FILE", "Filing Cabinet", 140), ("WB-GLASS", "Glass Whiteboard", 60),
]
LEADS = ["XYZ Interiors", "Al Khuwair Developers", "Barka Fit-Out Works"]


def seed_master(admin: AdminSession) -> None:
    for name in CUSTOMERS:
        if not admin.exists("Customer", name):
            admin.insert("Customer", {"customer_name": name, "customer_type": "Company", "customer_group": "Commercial",
                                      "territory": "Oman"})
            print(f"  customer {name}")
    for code, name, price in ITEMS:
        if not admin.exists("Item", code):
            admin.insert("Item", {"item_code": code, "item_name": name, "item_group": "Products", "stock_uom": "Nos",
                                  "is_stock_item": 0, "is_sales_item": 1, "standard_rate": price})
            print(f"  item {code}")
        if not admin.request("GET", "/api/resource/Item Price", params={
                "filters": f'[["item_code","=","{code}"],["price_list","=","Standard Selling"]]'})["data"]:
            admin.insert("Item Price", {"item_code": code, "price_list": "Standard Selling", "selling": 1, "price_list_rate": price})
    for company in LEADS:
        if not admin.request("GET", "/api/resource/Lead", params={"filters": f'[["company_name","=","{company}"]]'})["data"]:
            admin.insert("Lead", {"company_name": company, "lead_name": company, "status": "Open", "source": "Reference"})
            print(f"  lead {company}")


def empty(erp: ERPClient, doctype: str) -> bool:
    return not erp.get_list(doctype, [], ["name"], 1)


def lead_id(erp: ERPClient, company: str) -> str:
    return erp.get_list("Lead", [["company_name", "=", company]], ["name"], 1)[0]["name"]


def doc_with(erp, doctype, doc, submit=False):
    saved = erp.insert(doctype, doc)
    if submit:
        saved = erp.submit(doctype, saved["name"])
    return saved


def seed_transactions(users: dict[str, ERPClient]) -> None:
    alice, bob, manager = users["alice"], users["bob"], users["manager"]
    if empty(manager, "Opportunity"):
        for erp, party_type, party, amount, stage in [
                (alice, "Lead", lead_id(alice, "XYZ Interiors"), 7000, "Proposal/Price Quote"),
                (bob, "Customer", "Gulf Interiors LLC", 8500, "Negotiation/Review"),
                (bob, "Lead", lead_id(bob, "Al Khuwair Developers"), 15000, "Qualification")]:
            erp.insert("Opportunity", {"opportunity_from": party_type, "party_name": party, "status": "Open",
                                       "opportunity_type": "Sales", "opportunity_amount": amount, "sales_stage": stage})
        print("  opportunities")

    price = {code: rate for code, _, rate in ITEMS}  # ERPNext does not fill rates on REST insert; set them from the price list

    def lines(*pairs):
        return [{"item_code": c, "qty": q, "rate": price[c]} for c, q in pairs]

    if empty(manager, "Quotation"):
        for erp, party, when, items, submit in [
                (alice, "Nizwa Builders", 33, lines(("DESK-EX", 4), ("CHAIR-ERG", 4)), True),
                (alice, "XYZ Traders", 20, lines(("WS-4", 20)), True),
                (bob, "Gulf Interiors LLC", 12, lines(("TBL-CONF", 2), ("CHAIR-ERG", 12)), True),
                (bob, "Muscat Office Solutions", 5, lines(("CAB-FILE", 6)), True),
                (alice, "Sohar Contracting", 2, lines(("WS-4", 8), ("CHAIR-ERG", 8)), False)]:
            doc_with(erp, "Quotation", {"quotation_to": "Customer", "party_name": party, "transaction_date": D(when),
                                        "valid_till": (TODAY + dt.timedelta(days=30)).isoformat(), "order_type": "Sales",
                                        "items": items}, submit)
        print("  quotations")
    if empty(manager, "Sales Order"):
        for erp, party, when, items, submit in [
                (manager, "Nizwa Builders", 28, lines(("DESK-EX", 4), ("CHAIR-ERG", 4)), True),
                (manager, "Gulf Interiors LLC", 3, lines(("TBL-CONF", 2), ("CHAIR-ERG", 12)), True),
                (alice, "XYZ Traders", 1, lines(("WS-4", 20)), False)]:
            delivery = (TODAY + dt.timedelta(days=20)).isoformat()
            doc_with(erp, "Sales Order", {"customer": party, "transaction_date": D(when), "delivery_date": delivery,
                                          "order_type": "Sales",
                                          "items": [{**i, "delivery_date": delivery} for i in items]}, submit)
        print("  sales orders")
    if empty(manager, "Sales Activity"):
        rows = [
            (alice, "Nizwa Builders", "Meeting", 33, "Completed", "Site visit; they need 4 executive desks and 4 chairs."),
            (alice, "Nizwa Builders", "Quotation Sent", 32, "Awaiting Client", "Quotation for 4 desks and 4 chairs sent."),
            (alice, "Nizwa Builders", "Order Finalized", 28, "Completed", "PO received, order confirmed."),
            (alice, "XYZ Traders", "Call", 22, "In Progress", "Discussed workstation requirements for the new floor."),
            (alice, "XYZ Traders", "Quotation Sent", 20, "Awaiting Client", "Quotation for 20 workstations sent."),
            (alice, "XYZ Traders", "BOQ Received", 9, "In Progress", "Client sent their BOQ; preparing the revised offer."),
            (bob, "Gulf Interiors LLC", "Meeting", 14, "Completed", "Showroom review with the design team."),
            (bob, "Gulf Interiors LLC", "Quotation Sent", 12, "Awaiting Client", "Sent quotation for a conference table and 12 chairs."),
            (bob, "Gulf Interiors LLC", "Order Finalized", 3, "Completed", "Order confirmed after final negotiation."),
            (bob, "Muscat Office Solutions", "Quotation Sent", 5, "Awaiting Client", "Quotation for 6 filing cabinets sent."),
            (bob, "Al Khuwair Developers", "Meeting", 4, "Open", "Intro meeting; interested in a full fit-out."),
            (alice, "XYZ Interiors", "Meeting", 1, "Awaiting Client", "Met today, sent quotation for 20 workstations, awaiting their BOQ."),
            (manager, "Salalah Hospitality Group", "Call", 6, "Open", "Account review call."),
        ]
        for erp, client, kind, days_ago, status, details in rows:
            kind_doc = "Customer" if erp.get_list("Customer", [["customer_name", "=", client]], ["name"], 1) else "Lead"
            ref = erp.get_list(kind_doc, [[("customer_name" if kind_doc == "Customer" else "company_name"), "=", client]], ["name"], 1)
            erp.insert("Sales Activity", {"client_name": client, "activity_type": kind, "activity_date": D(days_ago), "status": status,
                                          "details": details, "reference_doctype": kind_doc if ref else None,
                                          "reference_name": ref[0]["name"] if ref else None})
        print("  sales activities")


def main():
    settings = Settings.from_env()
    missing = [c for c in ("alice", "bob", "manager") if c not in settings.erp_users]
    if missing:
        raise SystemExit(f"ERP_USERS lacks {missing}; run scripts/setup_erpnext.py first")
    admin = AdminSession()
    print("Master data")
    seed_master(admin)
    users = {c: ERPClient(ERP_URL, *settings.erp_users[c]) for c in ("alice", "bob", "manager")}
    for c, erp in users.items():
        erp.whoami()
    print("Transactions")
    try:
        seed_transactions(users)
    except ERPError as exc:
        raise SystemExit(f"Seeding failed: {exc}")
    print("Done.")


if __name__ == "__main__":
    main()
