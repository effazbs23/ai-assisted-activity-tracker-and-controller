"""Create (idempotently) the Odoo user, group and ir.access rows used by the middleware, and update .env.

Runs as the admin account (ODOO_ADMIN_USERNAME/ODOO_ADMIN_PASSWORD, or the current ODOO_USERNAME/PASSWORD on first run).
Least privilege: no delete permission on anything, read-only on reference data.
"""
import os
import re
import secrets

from app.config import ROOT, Settings, load_dotenv
from app.odoo.client import OdooClient

LOGIN = "ai.assistant"
NAME = "AI Sales Assistant"
GROUP = "AI Assistant (Middleware)"

load_dotenv()
s = Settings.from_env()
s.odoo_username = os.environ.get("ODOO_ADMIN_USERNAME", s.odoo_username)
s.odoo_password = os.environ.get("ODOO_ADMIN_PASSWORD", s.odoo_password)
admin_user, admin_pass = s.odoo_username, s.odoo_password
o = OdooClient(s)
o.authenticate()


def one(model, domain):
    rows = o.search_read(model, domain, ["id"], limit=1)
    return rows[0]["id"] if rows else None


internal = one("res.groups", [["full_name", "=", "Role / User"]])
sales_all = one("res.groups", [["full_name", "=", "Sales / User: All Documents"]])
group = one("res.groups", [["name", "=", GROUP]])
if not group:
    group = o.create("res.groups", {"name": GROUP, "implied_ids": [(6, 0, [internal, sales_all])],
                                    "comment": "Technical group for the AI middleware. No delete rights."})
    print("created group", group)

# (model, operation, name). Only what the middleware calls; nothing can be deleted.
ACCESS = [
    ("res.partner", "r", "customers"),
    ("res.users", "r", "users (calendar organizer)"),
    ("product.product", "cru", "products"),
    ("product.template", "cru", "product templates"),
    ("crm.lead", "ru", "leads"),
    ("sale.order", "cru", "sales orders"),
    ("sale.order.line", "cru", "sales order lines"),
    ("calendar.event", "cru", "calendar events"),
    ("mail.activity", "cr", "log activities"),
    ("mail.activity.type", "r", "activity types"),
    ("ir.model", "r", "model lookup for activities"),
]
for model, op, label in ACCESS:
    name = f"AI Assistant: {label}"
    existing = o.search_read("ir.access", [["name", "=", name]], ["id", "operation"], limit=1)
    if existing:
        if existing[0]["operation"] != op:  # keep rows in sync when permissions change
            o._execute("ir.access", "write", [[existing[0]["id"]], {"operation": op}])
            print(f"  ir.access {model} -> [{op}]")
        continue
    o.create("ir.access", {"name": name, "model_id": one("ir.model", [["model", "=", model]]),
                           "group_id": group, "operation": op, "kind": "permission"})
    print(f"  ir.access {model} [{op}]")

# Odoo 20: a row with a group is always a *permission* (OR-ed); only group-less rows are *restrictions* (AND-ed, for
# everyone). Base "Role / User" already grants customer CRUD, so block it for this login only via a restriction.
for stale in o.search_read("ir.access", [["name", "=", "AI Assistant: no customer edits"]], ["id"]):
    o._execute("ir.access", "unlink", [[stale["id"]]])  # earlier mistaken version (group set => no effect)
RESTRICT = "AI Assistant: customers are read-only"
if not one("ir.access", [["name", "=", RESTRICT]]):
    o.create("ir.access", {"name": RESTRICT, "model_id": one("ir.model", [["model", "=", "res.partner"]]),
                           "operation": "cud",
                           "domain": f"[('id', '>', 0)] if user.login != '{LOGIN}' else [('id', '=', 0)]"})
    print("  ir.access restriction: res.partner create/update/delete blocked for", LOGIN)

password = secrets.token_urlsafe(24)
user = one("res.users", [["login", "=", LOGIN]])
if user:
    o._execute("res.users", "write", [[user], {"password": password}])
    print("reset password for existing user", user)
else:
    user = o.create("res.users", {"name": NAME, "login": LOGIN, "email": "ai.assistant@brainstation23.local",
                                  "password": password, "group_ids": [(6, 0, [group])], "tz": s.timezone})
    print("created user", user)

env_path = ROOT / ".env"
text = env_path.read_text()


def setvar(text, key, value):
    pat = re.compile(rf"^{key}=.*$", re.M)
    return pat.sub(f"{key}={value}", text) if pat.search(text) else text.rstrip("\n") + f"\n{key}={value}\n"


if "ODOO_ADMIN_USERNAME" not in text:  # keep admin creds for seeding / admin scripts
    text = setvar(text, "ODOO_ADMIN_USERNAME", admin_user)
    text = setvar(text, "ODOO_ADMIN_PASSWORD", admin_pass)
text = setvar(text, "ODOO_USERNAME", LOGIN)
text = setvar(text, "ODOO_PASSWORD", password)
env_path.write_text(text)
print(".env updated: ODOO_USERNAME =", LOGIN)
