from tests.conftest import tool_call
from tests.test_api import H


def say(client, text, sid="s1", headers=H):
    r = client.post("/v1/prompt", json={"message": text, "session_id": sid}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_prompt_to_order_with_confirmation(ctx):
    client, odoo, llm = ctx
    llm.queue = [tool_call("search_customers", {"query": "XYZ Traders"}),
                 tool_call("search_products", {"query": "workstation"}),
                 tool_call("propose_sales_order", {"customer_id": 6, "lines": [{"product_id": 11, "quantity": 20}]}),
                 {"content": "Draft order for 20 Workstations (10000). Reply yes to confirm."}]
    out = say(client, "Create an order for 20 workstations for XYZ Traders")
    assert out["pending_action"]["kind"] == "create_order"
    assert odoo.created == []  # nothing written yet
    done = say(client, "yes")
    assert "Draft order" in done["reply"] and len(odoo.created) == 1
    llm.queue = [{"content": "Nothing is pending."}]
    assert say(client, "yes")["pending_action"] is None and len(odoo.created) == 1  # no double write


def test_no_cancels(ctx):
    client, odoo, llm = ctx
    llm.queue = [tool_call("propose_sales_order", {"customer_id": 6, "lines": [{"product_id": 11, "quantity": 2}]}),
                 {"content": "Reply yes or no."}]
    say(client, "order 2 workstations for xyz")
    assert "Cancelled" in say(client, "no")["reply"]
    assert odoo.created == []


def test_tool_error_is_fed_back_not_executed(ctx):
    client, odoo, llm = ctx
    llm.queue = [tool_call("propose_sales_order", {"customer_id": 6, "lines": [{"product_id": 9999, "quantity": 1}]}),
                 {"content": "I could not find that product."}]
    out = say(client, "order a thing")
    assert out["pending_action"] is None and odoo.created == []
    tool_msg = [m for m in llm.seen[1] if m["role"] == "tool"][0]
    assert "NotFound" in tool_msg["content"]


def test_context_is_kept_and_isolated(ctx):
    client, _, llm = ctx
    llm.queue = [tool_call("search_customers", {"query": "XYZ Traders"}), {"content": "Found XYZ Traders."},
                 {"content": "ok"}, {"content": "ok"}, {"content": "ok"}]
    say(client, "find xyz traders")
    say(client, "and its quotations?")
    second = llm.seen[2]
    assert "find xyz traders" in str(second) and '"id": 6' in second[0]["content"]
    say(client, "hello", sid="other")
    assert "find xyz traders" not in str(llm.seen[3])  # different session
    say(client, "hi", headers={"X-API-Key": "k2"})
    assert "find xyz traders" not in str(llm.seen[4])  # different caller


def test_history_is_trimmed(ctx):
    client, _, llm = ctx
    llm.queue = [{"content": "ok"}] * 30
    for i in range(30):
        say(client, f"m{i}")
    assert len(llm.seen[-1]) <= 1 + 12


def test_confirm_order_privilege_enforced_in_agent(ctx):
    client, odoo, llm = ctx
    odoo.t["sale.order"].append({"id": 50, "name": "S1", "state": "draft"})
    llm.queue = [tool_call("propose_confirm_order", {"order_id": 50}), {"content": "not allowed"}]
    out = say(client, "confirm order 50", headers={"X-API-Key": "k2"})
    assert out["pending_action"] is None
    assert "PermissionError" in [m for m in llm.seen[1] if m["role"] == "tool"][0]["content"]


def test_prompt_requires_auth_and_valid_body(ctx):
    client, *_ = ctx
    assert client.post("/v1/prompt", json={"message": "hi"}).status_code == 401
    assert client.post("/v1/prompt", json={"message": ""}, headers=H).status_code == 422
    assert client.post("/v1/prompt", json={"message": "x" * 5000}, headers=H).status_code == 422


def test_empty_model_reply_is_retried(ctx):
    client, _, llm = ctx
    llm.queue = [{"content": ""}, {"content": "Hello!"}]
    assert say(client, "hi")["reply"] == "Hello!"


def test_never_asks_for_yes_when_proposal_failed(ctx):
    client, odoo, llm = ctx
    llm.queue = [tool_call("propose_sales_order", {"customer_id": 6, "lines": [{"product_id": 9999, "quantity": 1}]}),
                 {"content": "Ready to create it. Please reply **yes** to confirm."}]
    out = say(client, "order the thing")
    assert out["pending_action"] is None and "could not set that up" in out["reply"] and "yes" not in out["reply"].lower()


def test_lead_found_by_customer_name(ctx):
    client, odoo, _ = ctx
    odoo.t["crm.lead"].append({"id": 2, "name": "Showroom Fitout", "partner_id": [7, "XYZ Interiors"]})
    assert client.get("/v1/leads?q=showroom", headers=H).json()[0]["id"] == 2      # by lead name
    assert client.get("/v1/leads?q=interiors", headers=H).json()[0]["id"] == 2     # falls back to customer name
    assert client.get("/v1/leads?q=nobody", headers=H).json() == []


def test_prompt_states_the_real_currency(ctx):
    client, _, llm = ctx
    llm.queue = [{"content": "ok"}]
    say(client, "hi")
    assert "Amounts are in USD" in llm.seen[0][0]["content"]


def test_fake_confirmation_prompt_is_nudged_into_a_real_proposal(ctx):
    client, odoo, llm = ctx
    llm.queue = [{"content": "I will create the order. Reply yes to confirm."},   # no tool call: nothing pending
                 tool_call("propose_sales_order", {"customer_id": 6, "lines": [{"product_id": 11, "quantity": 1}]}),
                 {"content": "Draft order for 1 Workstation. Reply yes or no."}]
    out = say(client, "sell 1 workstation to xyz traders")
    assert out["pending_action"]["kind"] == "create_order" and odoo.created == []
    assert "[system]" in str(llm.seen[1])


def test_unbacked_confirmation_never_reaches_the_user(ctx):
    client, odoo, llm = ctx
    llm.queue = [{"content": "Reply yes to confirm."}, {"content": "Reply yes to confirm, really."}]
    out = say(client, "do something")
    assert out["pending_action"] is None and "could not prepare" in out["reply"] and "yes" not in out["reply"].lower().replace("nothing", "")
