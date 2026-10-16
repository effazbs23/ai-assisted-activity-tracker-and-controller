"""Remove transactional demo data (activities, orders, quotations, opportunities and extra leads) from ERPNext.

Master data (customers, items, prices, the seeded leads) is kept. Asks for --yes because it deletes records.

    venv/bin/python scripts/reset_demo.py --yes
"""
from __future__ import annotations

import sys

from _common import AdminSession  # noqa: E402  (also sets sys.path)

from scripts.seed_demo import LEADS

ORDER = ["Sales Activity", "Sales Order", "Quotation", "Opportunity"]  # children before parents


def delete_doc(admin: AdminSession, doctype: str, name: str) -> None:
    doc = admin.request("GET", f"/api/resource/{doctype}/{name}")["data"]
    if doc.get("docstatus") == 1:
        admin.request("POST", "/api/method/frappe.client.cancel", json={"doctype": doctype, "name": name})
    admin.request("DELETE", f"/api/resource/{doctype}/{name}")


def purge(admin: AdminSession, names: list[tuple[str, str]]) -> int:
    for doctype, name in names:
        delete_doc(admin, doctype, name)
    return len(names)


def main():
    if "--yes" not in sys.argv:
        raise SystemExit("This deletes demo transactions. Re-run with --yes.")
    admin = AdminSession()
    total = 0
    for doctype in ORDER:
        rows = admin.request("GET", f"/api/resource/{doctype}", params={"limit_page_length": 1000, "fields": '["name"]'})["data"]
        total += purge(admin, [(doctype, r["name"]) for r in rows])
        print(f"  {doctype}: removed {len(rows)}")
    leads = admin.request("GET", f"/api/resource/Lead", params={"limit_page_length": 1000, "fields": '["name","company_name"]'})["data"]
    extra = [("Lead", r["name"]) for r in leads if r["company_name"] not in LEADS]
    total += purge(admin, extra)
    print(f"  Lead: removed {len(extra)} non-demo leads")
    print(f"Done ({total} records).")


if __name__ == "__main__":
    main()
