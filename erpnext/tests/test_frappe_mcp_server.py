"""The generic ERPNext MCP server: per-caller identity, guards, and the real stdio process."""
import json
import subprocess
import sys

import pytest

from app.config import Settings
from mcp_server.frappe_server import TOOLS, Server
from tests.stub_erp import StubERPNext, by_caller


def make(**kw):
    stubs = {"alice": StubERPNext("alice@example.com"), "bob": StubERPNext("bob@example.com", deny={"Customer"}),
             "manager": StubERPNext("manager@example.com")}
    settings = Settings(max_page_size=2, confirm_callers={"manager"}, item_creators={"manager"}, **kw)
    return Server(settings, by_caller(**stubs)), stubs


def call(server, name, args, caller="alice"):
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args, "_meta": {"caller": caller} if caller else {}}})
    r = resp["result"]
    return r["isError"], r["content"][0]["text"]


def test_tools_are_annotated_and_nothing_can_delete():
    names = {t["name"] for t in TOOLS}
    assert not any("delete" in n or "call_method" in n for n in names)
    reads = {t["name"] for t in TOOLS if t["annotations"]["readOnlyHint"]}
    assert reads == {"list_docs", "get_doc", "count_docs", "describe_doctype", "run_report"}
    assert next(t for t in TOOLS if t["name"] == "cancel_doc")["annotations"]["destructiveHint"]


def test_runs_as_the_caller_not_a_shared_user():
    s, _ = make()
    assert call(s, "list_docs", {"doctype": "Customer"}, "alice")[0] is False
    err, text = call(s, "list_docs", {"doctype": "Customer"}, "bob")  # bob's stub denies Customer
    assert err and "denied access" in text


def test_no_caller_is_refused():
    s, _ = make()
    err, text = call(s, "list_docs", {"doctype": "Customer"}, caller="")
    assert err and "No caller identity" in text


def test_limit_capped_and_count():
    s, _ = make()
    assert len(json.loads(call(s, "list_docs", {"doctype": "Customer", "limit": 500})[1])) == 2
    assert json.loads(call(s, "count_docs", {"doctype": "Customer"})[1]) == 3


def test_security_doctypes_denied():
    s, _ = make()
    for name, args in (("list_docs", {"doctype": "Role"}), ("create_doc", {"doctype": "DocPerm", "data": {}}),
                       ("create_doc", {"doctype": "User", "data": {}}), ("update_doc", {"doctype": "User", "name": "x", "data": {}})):
        err, text = call(s, name, args)
        assert err and "not available" in text


def test_users_are_readable_but_not_writable():
    s, stubs = make()
    stubs["alice"].db["User"] = [{"name": "alice@example.com", "full_name": "Alice"}]
    assert json.loads(call(s, "list_docs", {"doctype": "User", "fields": ["name", "full_name"]})[1])[0]["full_name"] == "Alice"


def test_privileged_actions_gated():
    s, stubs = make()
    assert call(s, "submit_doc", {"doctype": "Sales Order", "name": "x"}, "alice")[0]
    assert call(s, "cancel_doc", {"doctype": "Sales Order", "name": "x"}, "alice")[0]
    assert "Only authorised" in call(s, "create_doc", {"doctype": "Item", "data": {"item_code": "N"}}, "alice")[1]
    assert call(s, "create_doc", {"doctype": "Item", "data": {"item_code": "N"}}, "manager")[0] is False


def test_describe_and_create():
    s, stubs = make()
    stubs["alice"].db["DocType"] = [{"name": "Lead", "is_submittable": 0, "fields": [
        {"fieldname": "lead_name", "label": "Name", "fieldtype": "Data", "reqd": 1}, {"fieldtype": "Section Break"}]}]
    d = json.loads(call(s, "describe_doctype", {"doctype": "Lead"})[1])
    assert d["fields"] == [{"fieldname": "lead_name", "label": "Name", "fieldtype": "Data", "reqd": 1}]
    err, _ = call(s, "create_doc", {"doctype": "Lead", "data": {"lead_name": "Zed"}})
    assert not err and any(r.get("lead_name") == "Zed" for r in stubs["alice"].db["Lead"])


def test_real_process_lists_tools():
    p = subprocess.run([sys.executable, "-m", "mcp_server.frappe_server"], text=True, capture_output=True, timeout=30,
                       input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
                             '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n')
    out = [json.loads(l) for l in p.stdout.splitlines()]
    assert out[0]["result"]["serverInfo"]["name"] == "frappe"
    assert len(out[1]["result"]["tools"]) == len(TOOLS)
