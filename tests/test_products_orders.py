import pytest

from tests.conftest import tool_call
from tests.test_api import H

NEW = {"name": "Standing Desk Pro", "price": 1200, "description": "Electric, 140cm"}


def confirm(client, r):
    return client.post(f"/v1/actions/{r.json()['action_id']}/confirm", headers=H)


def test_create_product_is_two_step(ctx):
    client, odoo, _ = ctx
    r = client.post("/v1/products", json=NEW, headers=H)
    assert r.status_code == 202 and odoo.created == []
    done = confirm(client, r).json()["result"]
    assert done["name"] == "Standing Desk Pro" and done["list_price"] == 1200
    assert odoo.created[0][1]["description_sale"] == "Electric, 140cm"


@pytest.mark.parametrize("bad", [{"name": "X", "price": 5}, {"name": "Fine name", "price": 0},
                                 {"name": "Fine name", "price": -3}, {"name": "Fine name", "price": 10**9}])
def test_create_product_validation(ctx, bad):
    client, *_ = ctx
    assert client.post("/v1/products", json=bad, headers=H).status_code == 422


def test_duplicate_product_refused_case_insensitive(ctx):
    client, *_ = ctx
    r = client.post("/v1/products", json={"name": "workstation", "price": 9}, headers=H)
    assert r.status_code == 422 and "already exists" in r.json()["detail"]


def test_order_with_new_product_creates_both(ctx):
    client, odoo, _ = ctx
    body = {"customer_id": 6, "lines": [{"product_id": 11, "quantity": 2}, {"new_product": NEW, "quantity": 3}]}
    r = client.post("/v1/sales-orders", json=body, headers=H)
    assert r.status_code == 202
    pv = r.json()["preview"]
    assert pv["new_products"] == ["Standing Desk Pro"] and pv["subtotal_excl_tax"] == 2 * 500 + 3 * 1200
    assert odoo.created == []  # nothing written, not even the product
    result = confirm(client, r).json()["result"]
    kinds = [m for m, _ in odoo.created]
    assert kinds == ["product.product", "sale.order"] and len(result["created_product_ids"]) == 1
    assert result["amount_total"] == 4600


def test_order_line_must_have_exactly_one_source(ctx):
    client, *_ = ctx
    both = {"customer_id": 6, "lines": [{"product_id": 11, "new_product": NEW, "quantity": 1}]}
    none = {"customer_id": 6, "lines": [{"quantity": 1}]}
    assert client.post("/v1/sales-orders", json=both, headers=H).status_code == 422
    assert client.post("/v1/sales-orders", json=none, headers=H).status_code == 422


def test_new_product_in_order_cannot_duplicate_existing_or_itself(ctx):
    client, *_ = ctx
    dup = {"customer_id": 6, "lines": [{"new_product": {"name": "Workstation", "price": 1}, "quantity": 1}]}
    assert client.post("/v1/sales-orders", json=dup, headers=H).status_code == 422
    twice = {"customer_id": 6, "lines": [{"new_product": NEW, "quantity": 1}, {"new_product": NEW, "quantity": 2}]}
    assert client.post("/v1/sales-orders", json=twice, headers=H).status_code == 422


def test_failed_order_archives_products_it_created(ctx):
    client, odoo, _ = ctx
    r = client.post("/v1/sales-orders", json={"customer_id": 6, "lines": [{"new_product": NEW, "quantity": 1}]}, headers=H)
    real_create = odoo.create

    def boom(model, values):
        if model == "sale.order":
            raise RuntimeError("odoo down")
        return real_create(model, values)
    odoo.create = boom
    with pytest.raises(RuntimeError):
        confirm(client, r)
    assert odoo.written and odoo.written[0][2] == {"active": False}


def test_orders_of_a_customer_include_all_states(ctx):
    client, odoo, _ = ctx
    odoo.t["sale.order"] = [
        {"id": 1, "name": "S1", "state": "draft", "partner_id": [6, "XYZ Traders"], "date_order": "2026-10-01 00:00:00", "amount_total": 1},
        {"id": 2, "name": "S2", "state": "sale", "partner_id": [6, "XYZ Traders"], "date_order": "2026-10-02 00:00:00", "amount_total": 2},
        {"id": 3, "name": "S3", "state": "sale", "partner_id": [7, "XYZ Interiors"], "date_order": "2026-10-03 00:00:00", "amount_total": 3}]
    names = lambda q: sorted(o["name"] for o in client.get(f"/v1/sales-orders{q}", headers=H).json())
    assert names("?customer=traders") == ["S1", "S2"]
    assert names("?customer=traders&state=sale") == ["S2"]
    assert client.get("/v1/sales-orders?state=bogus", headers=H).status_code == 422
    assert client.get("/v1/sales-orders/999", headers=H).status_code == 404
    assert client.get("/v1/sales-orders/2", headers=H).json()["name"] == "S2"


def test_agent_creates_product_then_order_with_new_product(ctx):
    client, odoo, llm = ctx
    llm.queue = [tool_call("propose_create_product", {"name": "Standing Desk Pro", "price": 1200}),
                 {"content": "Create product Standing Desk Pro at 1200? yes/no"}]
    out = client.post("/v1/prompt", json={"message": "add product Standing Desk Pro 1200"}, headers=H).json()
    assert out["pending_action"]["kind"] == "create_product" and odoo.created == []
    assert "created" in client.post("/v1/prompt", json={"message": "yes"}, headers=H).json()["reply"]
    llm.queue = [tool_call("propose_sales_order", {"customer_id": 6, "lines": [
        {"quantity": 2, "new_product": {"name": "Glass Whiteboard", "price": 300}}]}), {"content": "Create it? yes/no"}]
    out = client.post("/v1/prompt", json={"message": "sell 2 glass whiteboards at 300 to xyz traders"}, headers=H).json()
    assert out["pending_action"]["preview"]["new_products"] == ["Glass Whiteboard"]
    done = client.post("/v1/prompt", json={"message": "yes"}, headers=H).json()
    assert "New product(s) created: 1" in done["reply"]


def test_agent_new_product_without_price_is_rejected_to_model(ctx):
    client, odoo, llm = ctx
    llm.queue = [tool_call("propose_sales_order", {"customer_id": 6, "lines": [
        {"quantity": 2, "new_product": {"name": "Glass Whiteboard"}}]}), {"content": "What price?"}]
    out = client.post("/v1/prompt", json={"message": "sell glass whiteboards"}, headers=H).json()
    assert out["pending_action"] is None and odoo.created == []
