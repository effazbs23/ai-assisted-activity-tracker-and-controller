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


SUPPLIERS = [("Gulf Timber Supplies", "Raw Material"), ("Oman Steel Works", "Raw Material"), ("Muscat Fabrics LLC", "Local"),
             ("Al Batinah Hardware", "Hardware")]
STOCK_ITEMS = [  # code, name, rate, qty in Stores, qty in Finished Goods
    ("BOLT-M8", "Hex Bolt M8", 0.15, 2000, 0), ("PANEL-MDF", "MDF Panel 18mm", 12.5, 150, 40),
    ("CASTOR-50", "Castor Wheel 50mm", 1.2, 600, 120), ("FABRIC-GRY", "Grey Office Fabric", 3.4, 300, 0),
]
PROJECTS = [("Nizwa Builders Fit-Out", "Open"), ("Gulf Interiors Showroom", "Open"), ("Muscat Office Refresh", "Completed")]
TASKS = [("Measure site", 0, "Completed"), ("Order materials", 0, "Working"), ("Install workstations", 0, "Open"),
         ("Design review", 1, "Working"), ("Deliver conference table", 1, "Open"), ("Handover and sign-off", 2, "Completed")]
EMPLOYEES = [("Aisha", "Al Balushi", "Female", "Sales"), ("Khalid", "Al Harthy", "Male", "Sales"), ("Maya", "Rawahi", "Female", "Sales"),
             ("Salim", "Al Lawati", "Male", "Accounts"), ("Fatma", "Al Kindi", "Female", "Human Resources")]
NOTES = [("Showroom visit checklist", "Bring catalogue, price list and sample fabrics."),
         ("Nizwa Builders follow-up", "Call the procurement lead about the desk delivery date."),
         ("Quarter targets", "Close two fit-out quotations before month end.")]
TODOS = ["Call Nizwa Builders about the delivery date", "Send the revised offer to XYZ Traders"]


def submit(admin: AdminSession, doctype: str, doc: dict) -> dict:
    saved = admin.insert(doctype, doc)
    return admin.request("POST", "/api/method/frappe.client.submit", json={"doc": saved})["message"]


def count(admin: AdminSession, doctype: str) -> int:
    return admin.request("GET", "/api/method/frappe.client.get_count", params={"doctype": doctype})["message"]


def seed_extra(admin: AdminSession, manager: ERPClient) -> None:
    """Data for the non-sales demo prompts (docs/prompts.csv): suppliers, stock, invoices, projects, employees, notes."""
    company = admin.request("GET", "/api/resource/Company")["data"][0]["name"]
    for name, group in SUPPLIERS:
        if not admin.exists("Supplier", name):
            admin.insert("Supplier", {"supplier_name": name, "supplier_group": group, "supplier_type": "Company"})
            print(f"  supplier {name}")
    for code, name, rate, *_ in STOCK_ITEMS:
        if not admin.exists("Item", code):
            admin.insert("Item", {"item_code": code, "item_name": name, "item_group": "Raw Material", "stock_uom": "Nos",
                                  "is_stock_item": 1, "is_sales_item": 0, "valuation_rate": rate, "standard_rate": rate})
            print(f"  stock item {code}")
    if count(admin, "Stock Entry") == 0:
        for warehouse, col in (("Stores - AMT", 3), ("Finished Goods - AMT", 4)):
            rows = [{"item_code": i[0], "qty": i[col], "basic_rate": i[2], "t_warehouse": warehouse} for i in STOCK_ITEMS if i[col]]
            submit(admin, "Stock Entry", {"stock_entry_type": "Material Receipt", "company": company, "posting_date": D(10), "items": rows})
        print("  stock receipts")
    if count(admin, "Sales Invoice") == 0:
        price = {code: rate for code, _, rate in ITEMS}
        for customer, when, due, items in [("Nizwa Builders", 25, 5, (("DESK-EX", 4), ("CHAIR-ERG", 4))),
                                           ("Gulf Interiors LLC", 8, -22, (("TBL-CONF", 2), ("CHAIR-ERG", 12))),
                                           ("Muscat Office Solutions", 4, -26, (("CAB-FILE", 6),))]:
            submit(admin, "Sales Invoice", {"customer": customer, "company": company, "posting_date": D(when), "set_posting_time": 1,
                                            "due_date": D(due), "items": [{"item_code": c, "qty": q, "rate": price[c]} for c, q in items]})
        print("  sales invoices (unpaid)")
    if count(admin, "Project") == 0:
        names = []
        for name, status in PROJECTS:
            names.append(admin.insert("Project", {"project_name": name, "status": status, "company": company,
                                                  "expected_start_date": D(20), "expected_end_date": (TODAY + dt.timedelta(days=40)).isoformat()})["name"])
        for subject, idx, status in TASKS:
            admin.insert("Task", {"subject": subject, "project": names[idx], "status": status, "priority": "Medium"})
        print("  projects and tasks")
    if count(admin, "Employee") == 0:
        for first, last, gender, dept in EMPLOYEES:
            department = f"{dept} - AMT"
            admin.insert("Employee", {"first_name": first, "last_name": last, "gender": gender, "company": company,
                                      "date_of_birth": "1990-05-12", "date_of_joining": D(400), "status": "Active",
                                      **({"department": department} if admin.exists("Department", department) else {})})
        print("  employees")
    if empty(manager, "Note"):
        for title, content in NOTES:
            manager.insert("Note", {"title": title, "content": content})
        print("  notes")
    if empty(manager, "ToDo"):
        for text in TODOS:
            manager.insert("ToDo", {"description": text, "priority": "Medium", "date": (TODAY + dt.timedelta(days=1)).isoformat()})
        print("  todos")
    if not manager.get_list("Sales Order", [["customer", "=", "Salalah Hospitality Group"]], ["name"], 1):
        delivery = (TODAY + dt.timedelta(days=15)).isoformat()
        doc = doc_with(manager, "Sales Order", {"customer": "Salalah Hospitality Group", "transaction_date": D(2), "delivery_date": delivery,
                                                "order_type": "Sales", "items": [{"item_code": "WB-GLASS", "qty": 5, "rate": 60,
                                                                                  "delivery_date": delivery}]}, True)
        print(f"  extra sales order {doc['name']} (safe to cancel in the demo)")


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
    print("Other areas (stock, invoices, projects, employees, notes)")
    try:
        seed_extra(admin, users["manager"])
    except (ERPError, RuntimeError) as exc:
        raise SystemExit(f"Extra seeding failed: {exc}")
    print("Done.")


if __name__ == "__main__":
    main()
