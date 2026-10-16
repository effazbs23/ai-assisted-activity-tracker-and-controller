from app.erp.client import ERPError

H = {"X-API-Key": "k1"}
HB = {"X-API-Key": "k2"}
HM = {"X-API-Key": "k3"}


def test_requires_api_key(ctx):
    client, *_ = ctx
    assert client.get("/v1/clients?q=nizwa").status_code == 401
    assert client.get("/v1/clients?q=nizwa", headers={"X-API-Key": "bad"}).status_code == 401
    assert client.get("/health").json() == {"status": "ok"}


def test_reads(ctx):
    client, *_ = ctx
    assert client.get("/v1/clients?q=nizwa", headers=H).json()[0]["type"] == "Customer"
    assert client.get("/v1/items?q=workstation", headers=H).json()[0]["list_price"] == 360
    assert client.get("/v1/opportunities", headers=H).json()[0]["id"] == "CRM-OPP-0001"
    assert len(client.get("/v1/quotations", headers=H).json()) == 3
    assert client.get("/v1/sales-orders?finalized=true", headers=H).json()[0]["name"] == "SAL-ORD-0001"
    assert client.get("/v1/quotations/SAL-QTN-0001", headers=H).json()["items"][0]["item_code"] == "WS-4"
    assert client.get("/v1/quotations/NOPE", headers=H).status_code == 502  # ERPNext 404 -> upstream error
    s = client.get("/v1/summary?doctype=Quotation", headers=H).json()
    assert s["count"] == 3 and s["total"] == 1750 and s["currency"] == "OMR"
    assert client.get("/v1/summary?doctype=Lead", headers=H).status_code == 422
    assert client.get("/v1/vocabulary", headers=H).json()["customers"]


def test_date_filters_and_bad_range(ctx):
    client, *_ = ctx
    import datetime as dt
    d = lambda n: (dt.date.today() - dt.timedelta(days=n)).isoformat()
    recent = client.get(f"/v1/quotations?date_from={d(7)}", headers=H).json()
    assert {q["name"] for q in recent} == {"SAL-QTN-0002", "SAL-QTN-0003"}
    assert client.get(f"/v1/quotations?date_from={d(1)}&date_to={d(5)}", headers=H).status_code == 422


def test_activities_filter(ctx):
    client, *_ = ctx
    assert [a["name"] for a in client.get("/v1/activities?activity_type=Meeting", headers=H).json()] == ["SACT-00001"]
    assert len(client.get("/v1/activities?mine=true", headers=H).json()) == 1   # alice's own
    assert client.get("/v1/activities?client=xyz", headers=H).json()[0]["name"] == "SACT-00002"
    assert client.get("/v1/activities?activity_type=Dancing", headers=H).status_code == 422
    assert client.get("/v1/activities?activity_type=boq", headers=H).status_code == 200  # aliases are accepted


def test_proposal_confirm_flow_and_idempotency(ctx):
    client, erp, _ = ctx
    body = {"activity_type": "Meeting", "details": "Showroom review", "party_type": "Customer", "party_id": "Nizwa Builders"}
    r = client.post("/v1/activities", json=body, headers={**H, "Idempotency-Key": "abc"})
    assert r.status_code == 202 and erp.created == []
    again = client.post("/v1/activities", json=body, headers={**H, "Idempotency-Key": "abc"})
    assert again.json()["action_id"] == r.json()["action_id"]
    aid = r.json()["action_id"]
    assert client.post(f"/v1/actions/{aid}/confirm", headers=HB).status_code == 404   # bound to the caller
    done = client.post(f"/v1/actions/{aid}/confirm", headers=H)
    assert done.status_code == 200 and done.json()["result"]["id"].startswith("SACT-")
    assert client.post(f"/v1/actions/{aid}/confirm", headers=H).json()["status"] == "done"  # replay is harmless
    assert len(erp.created) == 1


def test_cancel(ctx):
    client, erp, _ = ctx
    aid = client.post("/v1/quotations", json={"party_type": "Customer", "party_id": "XYZ Traders",
                                              "lines": [{"item_id": "WS-4", "quantity": 1}]}, headers=H).json()["action_id"]
    assert client.post(f"/v1/actions/{aid}/cancel", headers=H).json()["status"] == "cancelled"
    assert client.post(f"/v1/actions/{aid}/confirm", headers=H).status_code == 410
    assert erp.created == []


def test_validation_errors(ctx):
    client, *_ = ctx
    bad = client.post("/v1/quotations", json={"party_id": "XYZ Traders", "lines": [{"item_id": "NOPE", "quantity": 1}]}, headers=H)
    assert bad.status_code == 404
    assert client.post("/v1/quotations", json={"party_id": "XYZ Traders", "lines": [{"item_id": "WS-4", "quantity": 0}]},
                       headers=H).status_code == 422
    assert client.post("/v1/quotations", json={"party_id": "XYZ Traders", "lines": [{"item_id": "OLD-1", "quantity": 1}]},
                       headers=H).status_code == 404  # disabled items cannot be sold
    assert client.post("/v1/quotations", json={"party_id": "XYZ Traders", "lines": [{"item_id": "NOPRICE", "quantity": 1}]},
                       headers=H).status_code == 422  # no selling price in ERPNext
    assert client.post("/v1/sales-orders", json={"customer_id": "CRM-LEAD-0001", "lines": [{"item_id": "WS-4", "quantity": 1}]},
                       headers=H).status_code == 404  # a lead is not a customer


def test_only_confirm_callers_may_submit_orders_and_each_acts_as_own_user(ctx):
    client, alice, _ = ctx
    assert client.post("/v1/sales-orders/SAL-ORD-0001/submit", headers=H).status_code == 403
    manager = client.stubs["manager"]
    manager.db["Sales Order"].append({"name": "SAL-ORD-0009", "customer": "XYZ Traders", "customer_name": "XYZ Traders",
                                      "transaction_date": "2026-01-01", "grand_total": 5, "currency": "OMR", "status": "Draft",
                                      "docstatus": 0, "items": []})
    aid = client.post("/v1/sales-orders/SAL-ORD-0009/submit", headers=HM).json()["action_id"]
    out = client.post(f"/v1/actions/{aid}/confirm", headers=HM)
    assert out.status_code == 200 and out.json()["result"]["status"] == "To Deliver and Bill"
    assert alice.created == []  # the write went through the manager's ERPNext user only


def test_quotation_to_order_requires_submitted_quotation(ctx):
    client, erp, _ = ctx
    assert client.post("/v1/quotations/SAL-QTN-0002/to-order", headers=H).status_code == 422     # draft
    aid = client.post("/v1/quotations/SAL-QTN-0001/to-order", headers=H).json()["action_id"]
    out = client.post(f"/v1/actions/{aid}/confirm", headers=H).json()["result"]
    assert out["doctype"] == "Sales Order" and out["status"] == "Draft"
    assert any(c[0].endswith("make_sales_order") for c in erp.calls)


def test_opportunity_create_and_update(ctx):
    client, erp, _ = ctx
    aid = client.post("/v1/opportunities", json={"party_type": "Lead", "party_id": "CRM-LEAD-0001", "amount": 7000,
                                                 "sales_stage": "qualification", "notes": "from the call"}, headers=H).json()["action_id"]
    out = client.post(f"/v1/actions/{aid}/confirm", headers=H).json()["result"]
    assert out["sales_stage"] == "Qualification" and out["amount"] == 7000
    assert any(c[0].endswith("add_comment") for c in erp.calls)
    up = client.patch("/v1/opportunities/CRM-OPP-0001", json={"sales_stage": "Negotiation/Review"}, headers=H)
    aid2 = up.json()["action_id"]
    assert up.json()["preview"]["before"] == {"sales_stage": "Qualification"}
    assert client.post(f"/v1/actions/{aid2}/confirm", headers=H).json()["result"]["sales_stage"] == "Negotiation/Review"
    assert client.patch("/v1/opportunities/CRM-OPP-0001", json={"sales_stage": "Mars"}, headers=H).status_code == 422
    assert client.patch("/v1/opportunities/CRM-OPP-0001", json={}, headers=H).status_code == 422
    assert client.patch("/v1/opportunities/NOPE", json={"status": "Closed"}, headers=H).status_code == 404


def test_erpnext_down_gives_clean_error_and_nothing_is_lost(ctx):
    client, erp, _ = ctx
    aid = client.post("/v1/activities", json={"activity_type": "Call", "details": "x", "party_type": "Customer",
                                              "party_id": "XYZ Traders"}, headers=H).json()["action_id"]
    erp.unreachable = True
    r = client.post(f"/v1/actions/{aid}/confirm", headers=H)
    assert r.status_code == 502 and "unreachable" in r.json()["detail"]
    erp.unreachable = False
    assert client.post(f"/v1/actions/{aid}/confirm", headers=H).status_code == 200  # can be retried


def test_missing_erpnext_permission_is_reported(ctx):
    client, erp, _ = ctx
    erp.deny = {"Item"}
    r = client.get("/v1/items?q=chair", headers=H)
    assert r.status_code == 502 and "denied" in r.json()["detail"]


def test_prompt_validation_and_llm_outage(ctx):
    client, _, llm = ctx
    assert client.post("/v1/prompt", json={"message": ""}, headers=H).status_code == 422

    def boom(*a):
        raise RuntimeError("LLM unavailable: 429")
    llm.chat = boom
    assert client.post("/v1/prompt", json={"message": "hi"}, headers=H).status_code == 503


def test_rate_limit(tmp_path):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    from tests.stub_erp import StubERPNext, by_caller
    s = Settings(api_keys={"k1": "alice"}, audit_log_path=str(tmp_path / "a.log"), rate_limit_per_minute=3)
    c = TestClient(create_app(s, by_caller(alice=StubERPNext()), object()))
    codes = [c.get("/v1/clients?q=nizwa", headers=H).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_audit_log_records_events(ctx, tmp_path):
    client, *_ = ctx
    aid = client.post("/v1/activities", json={"activity_type": "Call", "details": "x", "party_type": "Customer",
                                              "party_id": "XYZ Traders"}, headers=H).json()["action_id"]
    client.post(f"/v1/actions/{aid}/confirm", headers=H)
    text = (tmp_path / "audit.log").read_text()
    assert '"event": "propose"' in text and '"event": "execute"' in text and '"caller": "alice"' in text


# -- creating items from chat/API ----------------------------------------------------------------------
def test_only_item_creators_may_create_items(ctx):
    client, alice, _ = ctx
    body = {"name": "Glass Whiteboard", "price": 300}
    assert client.post("/v1/items", json=body, headers=H).status_code == 403          # alice: not an item creator
    assert alice.created == []
    r = client.post("/v1/items", json=body, headers=HM)
    assert r.status_code == 202 and r.json()["preview"]["code"] == "GLASS-WHITEBOARD"
    manager = client.stubs["manager"]
    assert manager.created == []                                                      # a proposal writes nothing
    out = client.post(f"/v1/actions/{r.json()['action_id']}/confirm", headers=HM).json()["result"]
    assert out["id"] == "GLASS-WHITEBOARD" and [t for t, _ in manager.created] == ["Item"]


def test_item_validation_over_the_api(ctx):
    client, *_ = ctx
    assert client.post("/v1/items", json={"name": "Glass Whiteboard", "price": 0}, headers=HM).status_code == 422
    assert client.post("/v1/items", json={"name": "Ergonomic Chair", "price": 90}, headers=HM).status_code == 422   # duplicate
    assert client.post("/v1/items", json={"name": "Ergonomic Chairs", "price": 90}, headers=HM).status_code == 422  # look-alike
    assert client.post("/v1/items", json={"name": "Ergonomic Chairs", "price": 90, "allow_similar": True}, headers=HM).status_code == 202


def test_inline_item_in_a_quotation_over_the_api(ctx):
    client, alice, _ = ctx
    lines = [{"item_id": "WS-4", "quantity": 1}, {"new_item": {"name": "Glass Whiteboard", "price": 300}, "quantity": 2}]
    body = {"party_type": "Customer", "party_id": "XYZ Traders", "lines": lines}
    assert client.post("/v1/quotations", json=body, headers=H).status_code == 403     # alice may not create items inline either
    r = client.post("/v1/quotations", json=body, headers=HM)
    assert r.status_code == 202 and r.json()["preview"]["total_before_tax"] == 360 + 600
    done = client.post(f"/v1/actions/{r.json()['action_id']}/confirm", headers=HM).json()["result"]
    assert done["created_items"] == ["GLASS-WHITEBOARD"]
    bad = {"party_type": "Customer", "party_id": "XYZ Traders", "lines": [{"quantity": 1}]}
    assert client.post("/v1/quotations", json=bad, headers=HM).status_code == 422


def test_item_creation_rolled_back_when_the_document_is_refused(ctx):
    client, _, _ = ctx
    manager = client.stubs["manager"]
    body = {"party_type": "Customer", "party_id": "XYZ Traders",
            "lines": [{"new_item": {"name": "Glass Whiteboard", "price": 300}, "quantity": 1}]}
    aid = client.post("/v1/quotations", json=body, headers=HM).json()["action_id"]
    manager.deny_insert = {"Quotation"}
    r = client.post(f"/v1/actions/{aid}/confirm", headers=HM)
    assert r.status_code == 502 and "removed again" in r.json()["detail"]
    assert not [i for i in manager.db["Item"] if i["name"] == "GLASS-WHITEBOARD"]
