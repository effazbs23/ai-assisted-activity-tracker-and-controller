"""Integration tests against a real, running ERPNext seeded with scripts/seed_demo.py.

Skipped automatically when ERPNext or the .env credentials are not available. Run with:
    venv/bin/pytest tests/live -v
These tests create a few draft records (prefixed 'LIVE TEST') in ERPNext.
"""
import datetime as dt

import pytest
import requests

from app.config import Settings
from app.erp.client import ERPClient, ERPError
from app.services import activities, parties, proposals, sales
from app.services.errors import NotFound, ValidationFailed
from app.store.actions import ActionStore
from app.services import executor

S = Settings.from_env()


def _reachable() -> bool:
    try:
        return requests.get(f"{S.erp_url}/api/method/ping", timeout=3).ok and {"alice", "bob", "manager"} <= set(S.erp_users)
    except requests.RequestException:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="ERPNext not running or .env not prepared (scripts/setup_erpnext.py)")


@pytest.fixture(scope="module")
def users():
    return {c: ERPClient(S.erp_url, *S.erp_users[c], timeout=S.erp_timeout) for c in ("alice", "bob", "manager")}


TEST_DOCTYPES = ["Sales Activity", "Sales Order", "Quotation", "Opportunity", "Lead", "Item Price", "Item"]  # documents before the items they use
SUBMITTABLE = {"Sales Order", "Quotation"}


@pytest.fixture(scope="module", autouse=True)
def remove_records_created_by_these_tests(users):
    """Snapshot before, delete whatever the tests created afterwards (needs the admin login from .env)."""
    manager = users["manager"]
    before = {d: {r["name"] for r in manager.get_list(d, [], ["name"], 5000)} for d in TEST_DOCTYPES}
    yield
    import os
    admin = requests.Session()
    login = admin.post(f"{S.erp_url}/api/method/login", json={"usr": os.environ.get("ERP_ADMIN_USERNAME", "Administrator"),
                                                              "pwd": os.environ.get("ERP_ADMIN_PASSWORD", "admin")})
    if not login.ok:
        return
    for d in TEST_DOCTYPES:
        for r in manager.get_list(d, [], ["name", "docstatus"] if d in SUBMITTABLE else ["name"], 5000):
            if r["name"] in before[d]:
                continue
            if r.get("docstatus") == 1:
                admin.post(f"{S.erp_url}/api/method/frappe.client.cancel", json={"doctype": d, "name": r["name"]})
            admin.delete(f"{S.erp_url}/api/resource/{d}/{r['name']}")


class State:
    """Minimal stand-in for app.state with real ERPNext clients."""
    def __init__(self, users):
        self.settings = S
        S.item_creators = S.item_creators or {"manager"}
        self.erp_for = lambda caller: users[caller]
        self.actions = ActionStore(900)

        class Audit:
            def write(self, *a, **k): pass
        self.audit = Audit()


@pytest.fixture(scope="module")
def st(users):
    return State(users)


def test_each_user_authenticates_as_their_own_erpnext_user(users):
    assert users["alice"].whoami() == "alice@example.com"
    assert users["manager"].whoami() == "manager@example.com"


def test_client_search_exact_and_fuzzy(users):
    erp = users["alice"]
    exact = parties.search_clients(erp, "nizwa builders", 5)
    assert exact and exact[0]["name"] == "Nizwa Builders" and exact[0]["type"] == "Customer" and "match" not in exact[0]
    lead = parties.search_clients(erp, "XYZ Interiors", 5)
    assert lead[0]["type"] == "Lead"
    near = parties.search_clients(erp, "Nazwa Builders", 5)  # misheard
    assert near[0]["match"] == "fuzzy" and near[0]["name"] == "Nizwa Builders"
    assert parties.search_clients(erp, "Completely Unknown Org", 5) == []


def test_items_have_prices_from_erpnext(users):
    items = sales.search_items(users["alice"], "workstations", 5)
    assert items[0]["id"] == "WS-4" and items[0]["list_price"] == 350


def test_quotation_preview_uses_erpnext_prices_and_rejects_bad_input(users):
    erp = users["alice"]
    p = sales.build_preview(erp, "Quotation", "Customer", "Nizwa Builders",
                            [{"item_id": "DESK-EX", "quantity": 4}, {"item_id": "CHAIR-ERG", "quantity": 4}])
    assert p["total_before_tax"] == 4 * 420 + 4 * 95 and p["currency"] == "OMR"
    with pytest.raises(NotFound):
        sales.build_preview(erp, "Quotation", "Customer", "Nizwa Builders", [{"item_id": "NOPE", "quantity": 1}])
    with pytest.raises(ValidationFailed):
        sales.build_preview(erp, "Quotation", "Customer", "Nizwa Builders", [{"item_id": "WS-4", "quantity": 0}])
    with pytest.raises(ValidationFailed):  # sales orders need a real customer, not a lead
        sales.build_preview(erp, "Sales Order", "Lead", "XYZ Interiors", [{"item_id": "WS-4", "quantity": 1}])


def test_lists_and_summary_answer_questions(users):
    erp = users["manager"]
    today = dt.date.today()
    last_month_start = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    last_month_end = today.replace(day=1) - dt.timedelta(days=1)
    quotes = sales.list_documents(erp, "Quotation", 50, date_from=last_month_start, date_to=last_month_end)
    assert all(last_month_start.isoformat() <= q["transaction_date"] <= last_month_end.isoformat() for q in quotes)
    orders = sales.list_documents(erp, "Sales Order", 50, finalized=True)
    assert orders and all(o["docstatus"] == 1 for o in orders)
    total = sales.summary(erp, "Quotation", None, None)
    assert total["count"] >= 5 and total["total"] > 0 and total["currency"] == "OMR"
    nizwa = sales.summary(erp, "Quotation", None, None, customer="Nizwa Builders")
    assert 1 <= nizwa["count"] < total["count"]
    detail = sales.get_document(erp, "Quotation", quotes[0]["name"]) if quotes else None
    if detail:
        assert detail["items"]


def test_activity_visibility_own_vs_manager(users):
    mine = activities.list_activities(users["alice"], 100)
    assert mine and all(a["owner"] == "alice@example.com" for a in mine)  # ERPNext 'if owner' permission
    everyone = {a["owner"] for a in activities.list_activities(users["manager"], 100)}
    assert {"alice@example.com", "bob@example.com"} <= everyone  # manager sees the combined view
    only_bob_view = activities.list_activities(users["manager"], 100, mine=True)
    assert all(a["owner"] == "manager@example.com" for a in only_bob_view)


def run(st, caller, action):
    return executor.execute(st, action)


def test_log_activity_for_existing_client_and_new_client(st, users):
    p = proposals.log_activity(st, "alice", "meeting", "LIVE TEST met about desks", "waiting", None, "Customer", "Sohar Contracting")
    assert p.payload["activity_type"] == "Meeting" and p.payload["status"] == "Awaiting Client"
    out = run(st, "alice", p)
    assert out["id"].startswith("SACT-") and out["lead_created"] is None
    new = proposals.log_activity(st, "alice", "Quotation Sent", "LIVE TEST new client", "Awaiting Client",
                                 new_client_name="LIVE TEST Corp")
    out2 = run(st, "alice", new)
    assert out2["lead_created"]
    lead = users["alice"].get_list("Lead", [["name", "=", out2["lead_created"]]], ["company_name"], 1)
    assert lead[0]["company_name"] == "LIVE TEST Corp"
    with pytest.raises(NotFound):
        proposals.log_activity(st, "alice", "Meeting", "x", party_type="Customer", party_id="Not A Customer")
    with pytest.raises(ValidationFailed):
        proposals.log_activity(st, "alice", "Dancing", "x", party_type="Customer", party_id="Sohar Contracting")


def test_quotation_lifecycle_draft_submit_to_order_confirm(st, users):
    q = run(st, "alice", proposals.quotation(st, "alice", "Customer", "Muscat Office Solutions",
                                             [{"item_id": "CAB-FILE", "quantity": 3}]))
    assert q["doctype"] == "Quotation" and q["grand_total"] >= 420
    with pytest.raises(ValidationFailed):  # still a draft, so it cannot become an order yet
        proposals.quotation_to_order(st, "alice", q["id"])
    sub = run(st, "alice", proposals.submit_document(st, "alice", "Quotation", q["id"]))
    assert sub["status"] == "Open"
    so = run(st, "alice", proposals.quotation_to_order(st, "alice", q["id"]))
    assert so["doctype"] == "Sales Order" and so["status"] == "Draft"
    with pytest.raises(PermissionError):  # only CONFIRM_CALLERS may finalize orders
        proposals.submit_document(st, "alice", "Sales Order", so["id"])
    done = run(st, "manager", proposals.submit_document(st, "manager", "Sales Order", so["id"]))
    assert done["status"] == "To Deliver and Bill"
    with pytest.raises(ValidationFailed):  # no longer a draft
        proposals.submit_document(st, "manager", "Sales Order", so["id"])


def test_opportunity_create_and_update(st, users):
    opp = run(st, "bob", proposals.create_opportunity(st, "bob", "Customer", "Sohar Contracting", 4200, "Qualification", None,
                                                       "LIVE TEST note"))
    assert opp["sales_stage"] == "Qualification" and opp["amount"] == 4200
    upd = run(st, "bob", proposals.update_opportunity(st, "bob", opp["id"], sales_stage="proposal/price quote", amount=5000))
    assert upd["sales_stage"] == "Proposal/Price Quote" and upd["amount"] == 5000
    with pytest.raises(ValidationFailed):
        proposals.update_opportunity(st, "bob", opp["id"], sales_stage="Teleporting")
    found = parties.search_opportunities(users["bob"], "Sohar", 5)
    assert any(o["id"] == opp["id"] for o in found)


def test_manager_creates_a_nonstock_item_with_a_price(st, users):
    out = run(st, "manager", proposals.item(st, "manager", "LIVE TEST Glass Whiteboard", 300, "Wall mounted"))
    assert out["id"] == "LIVE-TEST-GLASS-WHITEBOARD" and out["list_price"] == 300
    item = users["manager"].get_doc("Item", out["id"])
    assert item["is_stock_item"] == 0 and item["is_sales_item"] == 1 and item["stock_uom"] == "Nos"
    # the selling price is now what quotations use, for a sales user who cannot create items
    assert sales.item_rates(users["alice"], [out["id"]]) == {out["id"]: 300}
    with pytest.raises(ValidationFailed, match="already exists"):
        proposals.item(st, "manager", "live test glass whiteboard", 300)
    with pytest.raises(PermissionError):
        proposals.item(st, "alice", "LIVE TEST Another Item", 10)


def test_inline_new_item_creates_item_price_and_quotation_together(st, users):
    action = proposals.quotation(st, "manager", "Customer", "Sohar Contracting", [
        {"item_id": "WS-4", "quantity": 2}, {"new_item": {"name": "LIVE TEST Bench", "price": 55.5}, "quantity": 4}])
    assert action.payload["total_before_tax"] == 2 * 350 + 4 * 55.5
    out = run(st, "manager", action)
    assert out["created_items"] == ["LIVE-TEST-BENCH"] and out["doctype"] == "Quotation"
    doc = users["manager"].get_doc("Quotation", out["id"])
    assert {i["item_code"]: i["rate"] for i in doc["items"]} == {"WS-4": 350, "LIVE-TEST-BENCH": 55.5}
    with pytest.raises(PermissionError):  # sales users cannot create items inline
        proposals.quotation(st, "bob", "Customer", "Sohar Contracting", [{"new_item": {"name": "LIVE TEST X", "price": 1}, "quantity": 1}])


def test_failed_inline_creation_leaves_no_orphan_item(st, users):
    action = proposals.order(st, "manager", "Nizwa Builders", [{"new_item": {"name": "LIVE TEST Orphan", "price": 9}, "quantity": 1}])
    action.payload["delivery_date"] = "1999-01-01"        # ERPNext refuses a delivery date before the order date
    with pytest.raises(ERPError):
        run(st, "manager", action)
    assert users["manager"].get_list("Item", [["name", "=", "LIVE-TEST-ORPHAN"]], ["name"], 1) == []


def test_erpnext_errors_surface_as_erperror(users):
    with pytest.raises(ERPError):
        users["alice"].get_doc("Quotation", "DOES-NOT-EXIST")
    bad = ERPClient(S.erp_url, "bad", "creds")
    with pytest.raises(ERPError):
        bad.whoami()
