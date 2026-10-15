"""Idempotently seed demo customers, products, leads, quotations and calendar events into the Odoo DB from .env."""
import datetime as dt
from zoneinfo import ZoneInfo

from app.config import Settings
from app.odoo.client import OdooClient

import os

s = Settings.from_env()
s.odoo_username = os.environ.get("ODOO_ADMIN_USERNAME", s.odoo_username)  # seeding needs admin rights
s.odoo_password = os.environ.get("ODOO_ADMIN_PASSWORD", s.odoo_password)
o = OdooClient(s)
o.authenticate()
tz = ZoneInfo(s.timezone)


def ensure(model, name, values, key="name"):
    rows = o.search_read(model, [[key, "=", name]], ["id"], limit=1)
    if rows:
        return rows[0]["id"]
    rid = o.create(model, {key: name, **values})
    print(f"  + {model}: {name}")
    return rid


customers = {
    "XYZ Traders": dict(email="demo@xyztraders.example", phone="+968 9000 0000", city="Muscat"),
    "Gulf Interiors LLC": dict(email="info@gulfinteriors.example", phone="+968 9111 2222", city="Muscat"),
    "Salalah Hospitality Group": dict(email="procure@salalahhg.example", phone="+968 9333 4444", city="Salalah"),
    "Nizwa Builders": dict(email="office@nizwabuilders.example", phone="+968 9555 6666", city="Nizwa"),
    "Sohar Tech Park": dict(email="admin@soharpark.example", phone="+968 9777 8888", city="Sohar"),
}
cid = {n: ensure("res.partner", n, {**v, "is_company": True}) for n, v in customers.items()}

products = {
    "Office Workstation": 500.0, "Executive Desk": 850.0, "Ergonomic Chair": 220.0,
    "Conference Table (12 seats)": 1800.0, "Filing Cabinet": 150.0, "Reception Counter": 2400.0,
    "Meeting Room Chair": 120.0, "Storage Locker": 95.0,
}
pid = {n: ensure("product.product", n, {"list_price": p, "sale_ok": True}) for n, p in products.items()}

leads = [
    ("XYZ Traders - Office Furniture Supply", "XYZ Traders", 25000),
    ("Gulf Interiors - Showroom Fitout", "Gulf Interiors LLC", 60000),
    ("Salalah Hospitality - Lobby Furniture", "Salalah Hospitality Group", 42000),
    ("Nizwa Builders - Site Office Setup", "Nizwa Builders", 12000),
    ("Sohar Tech Park - Co-working Furniture", "Sohar Tech Park", 90000),
]
for name, cust, rev in leads:
    ensure("crm.lead", name, {"partner_id": cid[cust], "expected_revenue": rev, "type": "opportunity"})

quotes = [  # (customer, [(product, qty)], days_ago, confirm)
    ("Gulf Interiors LLC", [("Executive Desk", 6), ("Ergonomic Chair", 12)], 3, False),
    ("Salalah Hospitality Group", [("Reception Counter", 1), ("Meeting Room Chair", 20)], 10, False),
    ("Nizwa Builders", [("Office Workstation", 8), ("Filing Cabinet", 8)], 40, False),
    ("Sohar Tech Park", [("Office Workstation", 30), ("Storage Locker", 30)], 20, True),
]
if not o.search_read("sale.order", [["partner_id", "=", cid["Gulf Interiors LLC"]]], ["id"], limit=1):
    for cust, lines, days_ago, confirm in quotes:
        when = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        oid = o.create("sale.order", {"partner_id": cid[cust], "date_order": when,
                       "order_line": [(0, 0, {"product_id": pid[p], "product_uom_qty": q}) for p, q in lines]})
        if confirm:
            o.call("sale.order", "action_confirm", [oid])
        print(f"  + sale.order for {cust} ({days_ago} days ago{', confirmed' if confirm else ''})")

me = o.search_read("res.users", [["id", "=", o.uid]], ["partner_id"])[0]["partner_id"][0]
tomorrow = dt.date.today() + dt.timedelta(days=1)
events = [("Weekly sales sync", 10, 0, 30), ("Lunch with Gulf Interiors", 13, 0, 60), ("Site visit - Nizwa Builders", 16, 0, 60)]
for name, h, m, dur in events:
    if o.search_read("calendar.event", [["name", "=", name]], ["id"], limit=1):
        continue
    start = dt.datetime.combine(tomorrow, dt.time(h, m), tz).astimezone(dt.timezone.utc)
    stop = start + dt.timedelta(minutes=dur)
    o.create("calendar.event", {"name": name, "start": start.strftime("%Y-%m-%d %H:%M:%S"),
             "stop": stop.strftime("%Y-%m-%d %H:%M:%S"), "partner_ids": [(6, 0, [me])], "user_id": o.uid})
    print(f"  + calendar.event: {name} tomorrow {h:02d}:{m:02d}")
print("seed complete")
