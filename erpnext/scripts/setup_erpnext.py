"""One-off ERPNext preparation for the assistant. Safe to re-run.

  1. Creates the 'Sales Activity' DocType (the structured log of what the team did).
  2. Creates three ERPNext users (two sales users, one manager) with their own API keys.
  3. Writes ERP_USERS / API_KEYS / CONFIRM_CALLERS into .env.

    venv/bin/python scripts/setup_erpnext.py
"""
from __future__ import annotations

import os
import secrets

from _common import ERP_URL, AdminSession, update_env  # noqa: E402  (also sets sys.path)

from app.erp.client import ERPClient, ERPError
from app.services.activities import ACTIVITY_TYPES, STATUSES

USERS = [  # caller, email, full name, roles (Sales Manager is additive on top of Sales User in ERPNext)
    ("alice", "alice@example.com", "Alice Salem", ["Sales User"]),
    ("bob", "bob@example.com", "Bob Hinai", ["Sales User"]),
    ("manager", "manager@example.com", "Maya Rawahi", ["Sales User", "Sales Manager"]),
]

SALES_ACTIVITY = {
    "doctype": "DocType", "name": "Sales Activity", "module": "Selling", "custom": 1, "autoname": "format:SACT-{#####}",
    "title_field": "client_name", "track_changes": 1, "sort_field": "activity_date", "sort_order": "DESC",
    "fields": [
        {"fieldname": "client_name", "label": "Client", "fieldtype": "Data", "reqd": 1, "in_list_view": 1, "in_standard_filter": 1},
        {"fieldname": "activity_type", "label": "Activity Type", "fieldtype": "Select", "reqd": 1, "in_list_view": 1,
         "in_standard_filter": 1, "options": "\n".join(ACTIVITY_TYPES)},
        {"fieldname": "activity_date", "label": "Date", "fieldtype": "Date", "reqd": 1, "in_list_view": 1,
         "in_standard_filter": 1, "default": "Today"},
        {"fieldname": "status", "label": "Status", "fieldtype": "Select", "reqd": 1, "in_list_view": 1,
         "in_standard_filter": 1, "default": "Open", "options": "\n".join(STATUSES)},
        {"fieldname": "column_break_1", "fieldtype": "Column Break"},
        {"fieldname": "reference_doctype", "label": "Linked To", "fieldtype": "Select", "options": "\nCustomer\nLead\nOpportunity"},
        {"fieldname": "reference_name", "label": "Linked Record", "fieldtype": "Dynamic Link", "options": "reference_doctype"},
        {"fieldname": "section_break_1", "fieldtype": "Section Break"},
        {"fieldname": "details", "label": "Details", "fieldtype": "Small Text"},
    ],
    # Sales users see and edit only their own records; managers see everything (the combined view).
    "permissions": [
        {"role": "Sales User", "read": 1, "write": 1, "create": 1, "if_owner": 1},
        {"role": "Sales Manager", "read": 1, "write": 1, "create": 1, "delete": 1, "report": 1, "export": 1, "share": 1},
        {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1, "report": 1, "export": 1, "share": 1},
    ],
}


def working(caller: str, creds: dict[str, tuple[str, str]]) -> bool:
    if caller not in creds:
        return False
    try:
        ERPClient(ERP_URL, *creds[caller]).whoami()
        return True
    except ERPError:
        return False


# Sales roles need to read selling prices (exact totals before anything is saved) and the list of sales stages.
EXTRA_READ_PERMISSIONS = [(dt, r) for dt in ("Item Price", "Sales Stage") for r in ("Sales User", "Sales Manager")]


def grant_read(admin: AdminSession, doctype: str, role: str) -> None:
    rows = admin.request("GET", "/api/resource/Custom DocPerm", params={
        "filters": f'[["parent","=","{doctype}"],["role","=","{role}"],["permlevel","=",0]]', "fields": '["name","read"]'})["data"]
    if rows:
        return
    admin.insert("Custom DocPerm", {"parent": doctype, "parenttype": "DocType", "parentfield": "permissions",
                                    "role": role, "permlevel": 0, "read": 1})
    print(f"Granted {role} read access to {doctype}")


def main():
    admin = AdminSession()
    for doctype, role in EXTRA_READ_PERMISSIONS:
        grant_read(admin, doctype, role)
    if admin.exists("DocType", "Sales Activity"):
        print("DocType 'Sales Activity' already exists")
    else:
        admin.insert("DocType", SALES_ACTIVITY)
        print("Created DocType 'Sales Activity'")

    existing = {}
    for triple in os.environ.get("ERP_USERS", "").split(","):
        parts = triple.strip().split(":")
        if len(parts) == 3:
            existing[parts[0]] = (parts[1], parts[2])
    creds = {}
    for caller, email, full_name, roles in USERS:
        if not admin.exists("User", email):
            admin.insert("User", {"email": email, "first_name": full_name.split()[0], "last_name": full_name.split()[-1],
                                  "send_welcome_email": 0, "enabled": 1, "user_type": "System User",
                                  "roles": [{"role": r} for r in roles]})
            print(f"Created user {email} ({', '.join(roles)})")
        else:
            have = {r["role"] for r in admin.request("GET", f"/api/resource/User/{email}")["data"].get("roles", [])}
            if set(roles) - have:
                admin.request("PUT", f"/api/resource/User/{email}", json={"roles": [{"role": r} for r in sorted(have | set(roles))]})
                print(f"Added roles {sorted(set(roles) - have)} to {email}")
        if working(caller, existing):
            creds[caller] = existing[caller]
            print(f"  {caller}: existing API credentials still work")
            continue
        secret = admin.request("POST", "/api/method/frappe.core.doctype.user.user.generate_keys", json={"user": email})["message"]["api_secret"]
        key = admin.request("GET", f"/api/resource/User/{email}")["data"]["api_key"]
        creds[caller] = (key, secret)
        print(f"  {caller}: new API credentials generated")

    api_keys = {}
    for pair in os.environ.get("API_KEYS", "").split(","):
        if ":" in pair:
            k, c = pair.split(":", 1)
            api_keys[c.strip()] = k.strip()
    for caller, *_ in USERS:
        api_keys.setdefault(caller, secrets.token_urlsafe(24))
    update_env({
        "ERP_URL": ERP_URL,
        "ERP_USERS": ",".join(f"{c}:{k}:{s}" for c, (k, s) in creds.items()),
        "API_KEYS": ",".join(f"{k}:{c}" for c, k in api_keys.items()),
        "CONFIRM_CALLERS": "manager",
        "MIDDLEWARE_KEYS": ",".join(f"{c}:{k}" for c, k in api_keys.items()),
    })
    print("Wrote ERP_USERS, API_KEYS, CONFIRM_CALLERS, MIDDLEWARE_KEYS to .env")


if __name__ == "__main__":
    main()
