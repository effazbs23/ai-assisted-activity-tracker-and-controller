from tests.conftest import tool_call

H = {"X-API-Key": "k1"}  # alice
HM = {"X-API-Key": "k3"}  # manager (may submit sales orders)


def say(client, text, sid="s1", headers=H):
    r = client.post("/v1/prompt", json={"message": text, "session_id": sid}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_daily_update_becomes_a_confirmed_activity_record(ctx):
    client, erp, llm = ctx
    llm.queue = [tool_call("search_clients", {"query": "XYZ Interiors"}),
                 tool_call("propose_log_activity", {"activity_type": "Quotation Sent", "status": "Awaiting Client",
                                                    "details": "Met today, sent quotation for 20 workstations, awaiting BOQ.",
                                                    "party_type": "Lead", "party_id": "CRM-LEAD-0001"}),
                 {"content": "I will log: Quotation Sent to XYZ Interiors, awaiting client. Reply yes to confirm."}]
    out = say(client, "Met with XYZ Interiors today, sent a quotation for 20 workstations, awaiting their BOQ.")
    assert out["pending_action"]["kind"] == "log_activity"
    assert out["pending_action"]["preview"]["client_name"] == "XYZ Interiors"
    assert erp.created == []  # nothing written before "yes"
    done = say(client, "yes")
    assert "Logged Quotation Sent with XYZ Interiors" in done["reply"]
    doctype, doc = erp.created[0]
    assert doctype == "Sales Activity" and doc["reference_doctype"] == "Lead" and doc["status"] == "Awaiting Client"
    assert doc["owner"] == "alice@example.com"  # written as alice's own ERPNext user
    llm.queue = [{"content": "Nothing is pending."}]
    assert say(client, "yes")["pending_action"] is None and len(erp.created) == 1  # no double write


def test_new_client_creates_a_lead_together_with_the_activity(ctx):
    client, erp, llm = ctx
    llm.queue = [tool_call("search_clients", {"query": "Brand New Fitouts"}),
                 tool_call("propose_log_activity", {"activity_type": "Meeting", "details": "Intro meeting",
                                                    "new_client_name": "Brand New Fitouts"}),
                 {"content": "New client, a lead will be created. Reply yes to confirm."}]
    say(client, "Met Brand New Fitouts today")
    done = say(client, "yes")
    assert [d for d, _ in erp.created] == ["Lead", "Sales Activity"]
    assert "New lead" in done["reply"]


def test_no_cancels(ctx):
    client, erp, llm = ctx
    llm.queue = [tool_call("propose_log_activity", {"activity_type": "Call", "details": "x", "party_type": "Customer",
                                                    "party_id": "XYZ Traders"}), {"content": "Reply yes or no."}]
    say(client, "called xyz traders")
    assert "Cancelled" in say(client, "no")["reply"]
    assert erp.created == []


def test_tool_error_is_fed_back_not_executed(ctx):
    client, erp, llm = ctx
    llm.queue = [tool_call("propose_create_quotation", {"party_type": "Customer", "party_id": "XYZ Traders",
                                                        "lines": [{"item_id": "NOPE", "quantity": 1}]}),
                 {"content": "I could not find that item."}]
    out = say(client, "quote a thing")
    assert out["pending_action"] is None and erp.created == []
    assert "NotFound" in [m for m in llm.seen[1] if m["role"] == "tool"][0]["content"]


def test_close_name_must_be_confirmed_before_it_can_be_used(ctx):
    client, erp, llm = ctx
    llm.queue = [tool_call("search_clients", {"query": "Nazwa Builders"}),
                 tool_call("propose_log_activity", {"activity_type": "Meeting", "details": "x", "party_type": "Customer",
                                                    "party_id": "Nizwa Builders"}, id="c2"),
                 {"content": "Did you mean Nizwa Builders?"}]
    out = say(client, "met nazwa builders")
    assert out["pending_action"] is None and "Did you mean" in out["reply"]
    results = [m["content"] for m in llm.seen[1] if m["role"] == "tool"]
    assert '"exact_match": false' in results[0]
    assert "close match suggested this turn" in [m["content"] for m in llm.seen[2] if m["role"] == "tool"][-1]


def test_rejected_suggestion_is_never_offered_again(ctx):
    client, erp, llm = ctx
    llm.queue = [tool_call("search_clients", {"query": "Nazwa Builders"}), {"content": "Did you mean Nizwa Builders?"},
                 tool_call("search_clients", {"query": "Nazwa Builders"}), {"content": "Is it a new client?"}]
    say(client, "met nazwa builders")
    say(client, "no")
    tool_msgs = [m["content"] for m in llm.seen[3] if m["role"] == "tool"]
    assert '"closest_matches": []' in tool_msgs[-1] and "Do NOT suggest them again" in tool_msgs[-1]


def test_quotation_then_order_permissions(ctx):
    client, erp, llm = ctx
    llm.queue = [tool_call("propose_create_quotation", {"party_type": "Customer", "party_id": "XYZ Traders",
                                                        "lines": [{"item_id": "WS-4", "quantity": 20}]}),
                 {"content": "Quotation total 7200 + tax. Reply yes."}]
    out = say(client, "quote 20 workstations for xyz traders")
    assert out["pending_action"]["preview"]["total_before_tax"] == 7200  # ERPNext Item Price (360), not the standard rate
    assert "Draft Quotation" in say(client, "yes")["reply"]
    # alice may not finalize a sales order: the tool reports it and no action is parked
    llm.queue = [tool_call("propose_submit_document", {"doctype": "Sales Order", "id": "SAL-ORD-0001"}),
                 {"content": "I cannot confirm orders."}]
    denied = say(client, "finalize SAL-ORD-0001")
    assert denied["pending_action"] is None
    assert "PermissionDenied" in [m["content"] for m in llm.seen[-1] if m["role"] == "tool"][0]


def test_context_is_kept_and_isolated(ctx):
    client, _, llm = ctx
    llm.queue = [tool_call("search_clients", {"query": "XYZ Traders"}), {"content": "Found XYZ Traders."},
                 {"content": "ok"}, {"content": "ok"}, {"content": "ok"}]
    say(client, "find xyz traders")
    say(client, "and its quotations?")
    assert "find xyz traders" in str(llm.seen[2]) and '"id": "XYZ Traders"' in llm.seen[2][0]["content"]
    say(client, "hello", sid="other")
    assert "find xyz traders" not in str(llm.seen[3])
    say(client, "hi", headers={"X-API-Key": "k2"})
    assert "find xyz traders" not in str(llm.seen[4])


def test_model_asking_for_yes_without_a_proposal_is_corrected(ctx):
    client, erp, llm = ctx
    llm.queue = [{"content": "I will log it. Reply yes to confirm."}, {"content": "Still sure? Reply yes to confirm."}]
    out = say(client, "log a meeting")
    assert out["pending_action"] is None and "Nothing was changed" in out["reply"]


def test_history_is_trimmed(ctx):
    client, _, llm = ctx
    llm.queue = [{"content": f"r{i}"} for i in range(30)]
    for i in range(20):
        say(client, f"message {i}")
    assert len(llm.seen[-1]) <= 13  # system + at most SESSION_MAX_MESSAGES
