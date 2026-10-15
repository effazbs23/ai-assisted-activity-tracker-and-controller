from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.fake_odoo import FakeOdoo


H = {"X-API-Key": "k1"}
ORDER = {"customer_id": 6, "lines": [{"product_id": 11, "quantity": 20}]}


def test_requires_api_key(ctx):
    client, *_ = ctx
    assert client.get("/v1/customers?q=xy").status_code == 401
    assert client.get("/v1/customers?q=xy", headers={"X-API-Key": "bad"}).status_code == 401


def test_read_endpoint(ctx):
    client, *_ = ctx
    r = client.get("/v1/customers?q=xyz", headers=H)
    assert r.status_code == 200 and r.json()[0]["name"] == "XYZ Traders"


def test_order_not_written_until_confirmed(ctx):
    client, odoo, _ = ctx
    r = client.post("/v1/sales-orders", json=ORDER, headers=H)
    assert r.status_code == 202
    assert r.json()["preview"]["subtotal_excl_tax"] == 10000.0
    assert odoo.created == []
    done = client.post(f"/v1/actions/{r.json()['action_id']}/confirm", headers=H)
    assert done.json()["result"]["name"].startswith("S")
    assert len(odoo.created) == 1
    # repeating confirm does not create a second order
    client.post(f"/v1/actions/{r.json()['action_id']}/confirm", headers=H)
    assert len(odoo.created) == 1


def test_idempotency_key_reuses_action(ctx):
    client, *_ = ctx
    h = {**H, "Idempotency-Key": "abc"}
    a = client.post("/v1/sales-orders", json=ORDER, headers=h).json()["action_id"]
    b = client.post("/v1/sales-orders", json=ORDER, headers=h).json()["action_id"]
    assert a == b


def test_validation_rejects_bad_input(ctx):
    client, *_ = ctx
    assert client.post("/v1/sales-orders", json={"customer_id": 6, "lines": []}, headers=H).status_code == 422
    bad_qty = {"customer_id": 6, "lines": [{"product_id": 11, "quantity": 99999}]}
    assert client.post("/v1/sales-orders", json=bad_qty, headers=H).status_code == 422
    assert client.post("/v1/sales-orders", json={**ORDER, "customer_id": 404}, headers=H).status_code == 404
    bad_prod = {"customer_id": 6, "lines": [{"product_id": 404, "quantity": 1}]}
    assert client.post("/v1/sales-orders", json=bad_prod, headers=H).status_code == 404


def test_actions_are_bound_to_caller(ctx):
    client, *_ = ctx
    aid = client.post("/v1/sales-orders", json=ORDER, headers=H).json()["action_id"]
    assert client.post(f"/v1/actions/{aid}/confirm", headers={"X-API-Key": "k2"}).status_code == 404


def test_cancel_blocks_execution(ctx):
    client, odoo, _ = ctx
    aid = client.post("/v1/sales-orders", json=ORDER, headers=H).json()["action_id"]
    client.post(f"/v1/actions/{aid}/cancel", headers=H)
    assert client.post(f"/v1/actions/{aid}/confirm", headers=H).status_code == 410
    assert odoo.created == []


def test_confirm_order_requires_privilege(ctx):
    client, odoo, _ = ctx
    assert client.post("/v1/sales-orders/1/confirm", headers={"X-API-Key": "k2"}).status_code == 403
    aid = client.post("/v1/sales-orders", json=ORDER, headers=H).json()["action_id"]
    oid = client.post(f"/v1/actions/{aid}/confirm", headers=H).json()["result"]["id"]
    cid = client.post(f"/v1/sales-orders/{oid}/confirm", headers=H).json()["action_id"]
    r = client.post(f"/v1/actions/{cid}/confirm", headers=H)
    assert r.json()["result"]["state"] == "sale"


def test_log_activity_flow(ctx):
    client, odoo, _ = ctx
    r = client.post("/v1/leads/1/activities", json={"summary": "Call with XYZ", "note": "n"}, headers=H)
    done = client.post(f"/v1/actions/{r.json()['action_id']}/confirm", headers=H)
    assert done.json()["result"]["activity_id"] > 100


def test_rate_limit(tmp_path):
    settings = Settings(api_keys={"k1": "alice"}, audit_log_path=str(tmp_path / "a.log"), rate_limit_per_minute=5)
    client = TestClient(create_app(settings, FakeOdoo(), None))
    codes = [client.get("/v1/leads", headers=H).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[-1] == 429


def test_page_size_capped(ctx):
    client, odoo, _ = ctx
    odoo.t["crm.lead"] = [{"id": i, "name": f"L{i}"} for i in range(200)]
    assert len(client.get("/v1/leads?limit=100000", headers=H).json()) == 50


def test_search_tolerates_plurals_and_word_order(ctx):
    client, *_ = ctx
    assert client.get("/v1/products?q=workstations", headers=H).json()[0]["name"] == "Workstation"
    assert client.get("/v1/customers?q=traders xyz", headers=H).json()[0]["name"] == "XYZ Traders"
    assert client.get("/v1/products?q=hidden", headers=H).json() == []  # not sellable
