import pytest

from app.services.search import FUZZY_THRESHOLD, similarity
from tests.conftest import tool_call
from tests.test_api import H


@pytest.mark.parametrize("heard,real", [
    ("nazwa builders", "Nizwa Builders"), ("Soha Tech Park", "Sohar Tech Park"), ("gulf interior", "Gulf Interiors LLC"),
    ("builders nizwa", "Nizwa Builders"), ("Salala Hospitality", "Salalah Hospitality Group"), ("xyz trader", "XYZ Traders")])
def test_misheard_names_score_above_threshold(heard, real):
    assert similarity(heard, real) >= FUZZY_THRESHOLD


@pytest.mark.parametrize("heard,other", [("nazwa builders", "XYZ Traders"), ("pizza", "Gulf Interiors LLC"),
                                         ("asdfgh", "Nizwa Builders")])
def test_unrelated_names_score_below_threshold(heard, other):
    assert similarity(heard, other) < FUZZY_THRESHOLD


def add_nizwa(odoo):
    odoo.t["res.partner"].append({"id": 20, "name": "Nizwa Builders", "email": "", "phone": ""})


def test_rest_returns_flagged_fuzzy_match_only_when_exact_is_empty(ctx):
    client, odoo, _ = ctx
    add_nizwa(odoo)
    exact = client.get("/v1/customers?q=nizwa", headers=H).json()
    assert exact[0]["name"] == "Nizwa Builders" and "match" not in exact[0]
    fuzzy = client.get("/v1/customers?q=nazwa builders", headers=H).json()
    assert fuzzy[0]["name"] == "Nizwa Builders" and fuzzy[0]["match"] == "fuzzy"
    assert client.get("/v1/customers?q=qqqqzzzz", headers=H).json() == []


def test_agent_asks_did_you_mean_instead_of_not_found(ctx):
    client, odoo, llm = ctx
    add_nizwa(odoo)
    llm.queue = [tool_call("search_customers", {"query": "Nazwa Builders"}),
                 {"content": "I couldn't find Nazwa Builders. Did you mean Nizwa Builders?"}]
    out = client.post("/v1/prompt", json={"message": "orders of nazwa builders"}, headers=H).json()
    tool_msg = [m for m in llm.seen[1] if m["role"] == "tool"][0]["content"]
    assert '"exact_match": false' in tool_msg and "Did you mean Nizwa Builders?" in tool_msg
    assert "Did you mean Nizwa Builders" in out["reply"]


def test_orders_of_misspelt_customer_returns_a_suggestion_not_empty(ctx):
    client, odoo, llm = ctx
    add_nizwa(odoo)
    llm.queue = [tool_call("list_sales_orders", {"customer": "nazwa builders"}), {"content": "Did you mean Nizwa Builders?"}]
    client.post("/v1/prompt", json={"message": "show orders of nazwa builders"}, headers=H)
    tool_msg = [m for m in llm.seen[1] if m["role"] == "tool"][0]["content"]
    assert "closest_matches" in tool_msg and "Nizwa Builders" in tool_msg


def test_fuzzy_customer_cannot_be_used_for_a_write_in_the_same_turn(ctx):
    client, odoo, llm = ctx
    add_nizwa(odoo)
    llm.queue = [tool_call("search_customers", {"query": "Nazwa Builders"}, id="a"),
                 tool_call("propose_sales_order", {"customer_id": 20, "lines": [{"product_id": 11, "quantity": 1}]}, id="b"),
                 {"content": "Did you mean Nizwa Builders?"}]
    out = client.post("/v1/prompt", json={"message": "sell a workstation to nazwa builders"}, headers=H).json()
    assert out["pending_action"] is None and odoo.created == []
    assert "close match" in str([m for m in llm.seen[2] if m["role"] == "tool"][-1])


def test_after_the_user_confirms_the_record_can_be_used(ctx):
    client, odoo, llm = ctx
    add_nizwa(odoo)
    llm.queue = [tool_call("search_customers", {"query": "Nazwa Builders"}), {"content": "Did you mean Nizwa Builders?"}]
    client.post("/v1/prompt", json={"message": "sell a workstation to nazwa builders"}, headers=H)
    llm.queue = [tool_call("search_customers", {"query": "Nizwa Builders"}, id="c"),
                 tool_call("propose_sales_order", {"customer_id": 20, "lines": [{"product_id": 11, "quantity": 1}]}, id="d"),
                 {"content": "Draft for Nizwa Builders. Reply yes or no."}]
    out = client.post("/v1/prompt", json={"message": "yes, Nizwa Builders"}, headers=H).json()
    assert out["pending_action"]["kind"] == "create_order" and odoo.created == []


def test_fuzzy_product_in_order_is_blocked_too(ctx):
    client, odoo, llm = ctx
    llm.queue = [tool_call("search_products", {"query": "workstatoin"}, id="a"),
                 tool_call("propose_sales_order", {"customer_id": 6, "lines": [{"product_id": 11, "quantity": 1}]}, id="b"),
                 {"content": "Did you mean Workstation?"}]
    out = client.post("/v1/prompt", json={"message": "sell 1 workstatoin to xyz traders"}, headers=H).json()
    assert out["pending_action"] is None


def test_similar_new_product_is_refused_unless_user_says_it_is_different(ctx):
    client, *_ = ctx
    r = client.post("/v1/products", json={"name": "Workstations", "price": 10}, headers=H)
    assert r.status_code == 422 and "very similar" in r.json()["detail"]
    ok = client.post("/v1/products", json={"name": "Workstations", "price": 10, "allow_similar": True}, headers=H)
    assert ok.status_code == 202
    assert client.post("/v1/products", json={"name": "Neon Sign", "price": 10}, headers=H).status_code == 202


def test_similar_check_also_applies_to_new_product_inside_an_order(ctx):
    client, *_ = ctx
    body = {"customer_id": 6, "lines": [{"new_product": {"name": "Workstatoin", "price": 9}, "quantity": 1}]}
    assert client.post("/v1/sales-orders", json=body, headers=H).status_code == 422


def test_vocabulary_endpoint(ctx):
    client, *_ = ctx
    d = client.get("/v1/vocabulary", headers=H).json()
    assert "XYZ Traders" in d["customers"] and "Workstation" in d["products"] and "XYZ Traders" not in d["products"]


def redmi(odoo):
    odoo.t["product.product"].append({"id": 30, "name": "Redmi Note 4", "list_price": 12000.0, "sale_ok": True, "default_code": ""})


def say(client, text, sid="r1"):
    r = client.post("/v1/prompt", json={"message": text, "session_id": sid}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def tool_results(llm, i):
    return [m["content"] for m in llm.seen[i] if m["role"] == "tool"]


def test_rejecting_a_suggestion_leads_to_creating_the_new_product(ctx):
    """'Redmi Note 6' -> 'Did you mean Redmi Note 4?' -> no  => ask to create it, never ask about Note 4 again."""
    client, odoo, llm = ctx
    redmi(odoo)
    llm.queue = [tool_call("search_products", {"query": "Redmi Note 6"}), {"content": "Did you mean Redmi Note 4?"}]
    assert "Did you mean Redmi Note 4" in say(client, "sell 1 redmi note 6 to xyz traders")["reply"]
    first = tool_results(llm, 1)[0]
    assert '"exact_match": false' in first and "Redmi Note 4" in first

    llm.queue = [tool_call("search_products", {"query": "Redmi Note 6"}),
                 {"content": "Redmi Note 6 doesn't exist. Create it as a new product? What price?"}]
    say(client, "no")
    again = tool_results(llm, 3)[0]  # the second search in the new turn
    assert '"closest_matches": []' in again and "Redmi Note 4" not in again and "NEW product" in again

    llm.queue = [tool_call("propose_sales_order", {"customer_id": 6, "lines": [
        {"quantity": 1, "new_product": {"name": "Redmi Note 6", "price": 15000}}]}), {"content": "Create it? yes/no"}]
    out = say(client, "15000")
    assert out["pending_action"]["kind"] == "create_order"
    assert out["pending_action"]["preview"]["new_products"] == ["Redmi Note 6"] and odoo.created == []


def test_new_product_is_still_blocked_if_the_user_never_rejected_the_suggestion(ctx):
    client, odoo, llm = ctx
    redmi(odoo)
    llm.queue = [tool_call("propose_sales_order", {"customer_id": 6, "lines": [
        {"quantity": 1, "new_product": {"name": "Redmi Note 6", "price": 15000}}]}), {"content": "Is it Redmi Note 4?"}]
    out = say(client, "sell redmi note 6 at 15000 to xyz traders")
    assert out["pending_action"] is None
    assert "very similar" in tool_results(llm, 1)[0]


def test_a_non_rejection_reply_does_not_mark_the_suggestion_rejected(ctx):
    client, odoo, llm = ctx
    redmi(odoo)
    llm.queue = [tool_call("search_products", {"query": "Redmi Note 6"}), {"content": "Did you mean Redmi Note 4?"}]
    say(client, "sell 1 redmi note 6 to xyz traders")
    llm.queue = [tool_call("search_products", {"query": "Redmi Note 6"}), {"content": "Did you mean Redmi Note 4?"}]
    say(client, "hmm what is its price?")  # not a "no"
    assert "Redmi Note 4" in tool_results(llm, 3)[0] and '"closest_matches": []' not in tool_results(llm, 3)[0]


@pytest.mark.parametrize("reply", ["no", "No.", "nope", "not that one", "a different product", "new product please"])
def test_various_ways_of_saying_no_are_recognised(ctx, reply):
    client, odoo, llm = ctx
    redmi(odoo)
    llm.queue = [tool_call("search_products", {"query": "Redmi Note 6"}), {"content": "Did you mean Redmi Note 4?"}]
    say(client, "sell redmi note 6")
    llm.queue = [tool_call("search_products", {"query": "Redmi Note 6"}), {"content": "Create it?"}]
    say(client, reply)
    assert '"closest_matches": []' in tool_results(llm, 3)[0]


def test_rejection_is_per_session_and_per_record(ctx):
    client, odoo, llm = ctx
    redmi(odoo)
    llm.queue = [tool_call("search_products", {"query": "Redmi Note 6"}), {"content": "Did you mean Redmi Note 4?"}]
    say(client, "sell redmi note 6", sid="a")
    llm.queue = [{"content": "ok"}, tool_call("search_products", {"query": "Redmi Note 6"}), {"content": "Did you mean?"}]
    say(client, "no", sid="a")
    say(client, "sell redmi note 6", sid="b")  # another conversation still gets the suggestion
    assert "Redmi Note 4" in tool_results(llm, len(llm.seen) - 1)[0]
