"""MCP fallback: the stdio client against a real child process, and the orchestrator's two-phase behaviour."""
import json
import shlex
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent.mcp import MCPError, MCPHub, parse_mcp_servers
from app.config import Settings
from app.main import create_app
from tests.conftest import tool_call
from tests.stub_erp import StubERPNext, by_caller

SERVER = Path(__file__).with_name("fake_mcp_server.py")


def command(log: Path) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(SERVER))} {shlex.quote(str(log))}"


def calls(log: Path) -> list[dict]:
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


class RecordingLLM:
    """Scripted replies; records the tool names offered on every call."""
    def __init__(self):
        self.queue, self.offered, self.seen = [], [], []

    def chat(self, messages, tools):
        self.offered.append([t["function"]["name"] for t in tools])
        self.seen.append(list(messages))
        return self.queue.pop(0)


def reply(text):
    return {"content": text, "tool_calls": []}


@pytest.fixture
def hub(tmp_path):
    h = MCPHub({"fake": command(tmp_path / "calls.log")}, timeout=10)
    yield h, tmp_path / "calls.log"
    h.close()


@pytest.fixture
def app_ctx(tmp_path):
    log = tmp_path / "calls.log"
    settings = Settings(api_keys={"k1": "alice"}, audit_log_path=str(tmp_path / "audit.log"), rate_limit_per_minute=1000,
                        mcp_servers={"fake": command(log)}, mcp_timeout=10)
    llm = RecordingLLM()
    app = create_app(settings, by_caller(alice=StubERPNext("alice@example.com")), llm)
    with TestClient(app) as client:  # context manager runs the lifespan, which closes the MCP servers
        yield client, llm, log, app
    assert all(c.proc is None for c in app.state.mcp._conns.values())


def prompt(client, text):
    r = client.post("/v1/prompt", headers={"X-API-Key": "k1"}, json={"message": text, "session_id": "s"})
    assert r.status_code == 200, r.text
    return r.json()


# -- config ----------------------------------------------------------------------------------------
def test_parse_mcp_servers():
    assert parse_mcp_servers("a=cmd x; b = other --flag ;bad; c__d=nope; =x; e=") == {"a": "cmd x", "b": "other --flag"}


def test_settings_read_mcp_env(monkeypatch):
    monkeypatch.setenv("MCP_SERVERS", "fs=npx -y server-fs /tmp")
    monkeypatch.setenv("MCP_TIMEOUT", "5")
    s = Settings.from_env()
    assert s.mcp_servers == {"fs": "npx -y server-fs /tmp"} and s.mcp_timeout == 5


def test_no_servers_means_no_hub(tmp_path):
    app = create_app(Settings(audit_log_path=str(tmp_path / "a.log")), by_caller(), RecordingLLM())
    assert app.state.mcp is None


# -- the stdio client ------------------------------------------------------------------------------
def test_hub_lists_paginated_tools_and_classifies_reads(hub):
    h, _ = hub
    names = {t["function"]["name"] for t in h.tools()}
    assert names == {f"mcp__fake__{n}" for n in ("get_weather", "create_note", "lookup", "wipe_all", "checkout_cart", "get_broken")}
    assert h.is_read_only("mcp__fake__get_weather")      # name prefix
    assert h.is_read_only("mcp__fake__lookup")           # readOnlyHint annotation
    assert not h.is_read_only("mcp__fake__create_note")  # unknown verb = write
    assert not h.is_read_only("mcp__fake__wipe_all")     # destructiveHint beats readOnlyHint
    assert not h.is_read_only("mcp__fake__checkout_cart")  # 'check' prefix needs an underscore


def test_hub_calls_a_tool_and_skips_notifications(hub):
    h, log = hub
    out = h.call("mcp__fake__get_weather", {"city": "Muscat"})
    assert out == {"server": "fake", "tool": "get_weather", "content": "Sunny in Muscat"}
    assert calls(log) == [{"name": "get_weather", "arguments": {"city": "Muscat"}, "meta": None}]


def test_hub_reports_tool_errors_and_unknown_tools(hub):
    h, _ = hub
    with pytest.raises(MCPError, match="backend down"):
        h.call("mcp__fake__get_broken", {})
    with pytest.raises(MCPError, match="unknown MCP tool"):
        h.call("mcp__fake__nope", {})


def test_hub_restarts_a_server_that_exited(hub):
    h, _ = hub
    h.tools()
    h._conns["fake"].proc.kill()
    h._conns["fake"].proc.wait()
    assert h.call("mcp__fake__lookup", {})["content"] == "lookup ok"


def test_broken_server_is_skipped_and_backed_off(tmp_path):
    h = MCPHub({"bad": "/definitely/not/a/command", "fake": command(tmp_path / "c.log")}, timeout=10)
    try:
        names = {t["function"]["name"] for t in h.tools()}
        assert names and all(n.startswith("mcp__fake__") for n in names)
        assert "bad" in h._broken_until
    finally:
        h.close()


# -- orchestrator: phase 1 first, MCP only as a fallback ------------------------------------------------
def test_builtin_tools_handle_request_without_mcp(app_ctx):
    client, llm, log, _ = app_ctx
    llm.queue = [reply("You have no meetings today.")]
    out = prompt(client, "Any meetings today?")
    assert out["reply"] == "You have no meetings today."
    assert len(llm.offered) == 1 and not any(n.startswith("mcp__") for n in llm.offered[0])
    assert calls(log) == []


def test_unable_reply_falls_back_to_read_only_mcp_tool(app_ctx):
    client, llm, log, _ = app_ctx
    llm.queue = [reply("I can't check the weather with my tools."),
                 tool_call("mcp__fake__get_weather", {"city": "Muscat"}),
                 reply("It is sunny in Muscat.")]
    out = prompt(client, "What's the weather in Muscat?")
    assert out["reply"] == "It is sunny in Muscat." and out["pending_action"] is None
    assert any(n.startswith("mcp__") for n in llm.offered[1])  # phase 2 offers the MCP tools
    assert calls(log) == [{"name": "get_weather", "arguments": {"city": "Muscat"}, "meta": {"caller": "alice"}}]  # a read runs at once


def test_mcp_write_waits_for_yes(app_ctx):
    client, llm, log, _ = app_ctx
    llm.queue = [reply("I cannot create notes."),
                 tool_call("mcp__fake__create_note", {"text": "call back Nizwa"}),
                 reply("I will create the note 'call back Nizwa'. Reply yes or no.")]
    out = prompt(client, "Make a note: call back Nizwa")
    assert out["pending_action"]["kind"] == "mcp_call"
    assert out["pending_action"]["preview"]["arguments"] == {"text": "call back Nizwa"}
    assert calls(log) == []  # nothing ran yet
    done = prompt(client, "yes")
    assert done["reply"].startswith("Done. MCP tool fake.create_note ran. create_note ok")
    assert calls(log) == [{"name": "create_note", "arguments": {"text": "call back Nizwa"}, "meta": {"caller": "alice"}}]


def test_mcp_write_cancelled_with_no(app_ctx):
    client, llm, log, _ = app_ctx
    llm.queue = [reply("I'm unable to do that."), tool_call("mcp__fake__wipe_all", {}),
                 reply("This will wipe everything. Reply yes or no.")]
    assert prompt(client, "wipe it all")["pending_action"]["kind"] == "mcp_call"  # destructive => needs yes
    assert prompt(client, "no")["reply"].startswith("Cancelled")
    assert calls(log) == []


def test_mcp_write_can_be_confirmed_over_rest(app_ctx):
    client, llm, log, _ = app_ctx
    llm.queue = [reply("I cannot do that."), tool_call("mcp__fake__create_note", {"text": "x"}),
                 reply("Reply yes or no.")]
    action = prompt(client, "note x")["pending_action"]
    r = client.post(f"/v1/actions/{action['id']}/confirm", headers={"X-API-Key": "k1"})
    assert r.status_code == 200 and r.json()["result"]["content"] == "create_note ok"
    assert len(calls(log)) == 1


def test_failing_mcp_read_is_reported_not_hidden(app_ctx):
    client, llm, _, _ = app_ctx
    llm.queue = [reply("I can't do that."), tool_call("mcp__fake__get_broken", {}),
                 reply("The external service failed: backend down.")]
    prompt(client, "use the broken thing")
    tool_msgs = [m for m in llm.seen[-1] if m["role"] == "tool"]
    assert "MCP: backend down" in tool_msgs[-1]["content"]  # the model is told the real error


def test_mcp_tools_are_not_callable_in_phase_one(app_ctx):
    client, llm, log, _ = app_ctx
    llm.queue = [tool_call("mcp__fake__create_note", {"text": "sneaky"}), reply("Sorry, that tool is unknown.")]
    prompt(client, "note sneaky")
    assert calls(log) == []  # phase 1 routes mcp__ names to the built-in dispatcher, which refuses them


def test_no_fallback_when_nothing_configured(tmp_path):
    llm = RecordingLLM()
    app = create_app(Settings(api_keys={"k1": "alice"}, audit_log_path=str(tmp_path / "a.log"), rate_limit_per_minute=1000),
                     by_caller(alice=StubERPNext("alice@example.com")), llm)
    llm.queue = [reply("I can't check the weather.")]
    with TestClient(app) as client:
        assert prompt(client, "weather?")["reply"] == "I can't check the weather."
    assert len(llm.offered) == 1


def test_caller_is_sent_with_reads_and_confirmed_writes(app_ctx):
    client, llm, log, _ = app_ctx
    llm.queue = [reply("I can't do that with my tools."),
                 tool_call("mcp__fake__get_weather", {"city": "Muscat"}), reply("Sunny."),
                 reply("I can't do that with my tools."), tool_call("mcp__fake__create_note", {"text": "hi"}), reply("Reply yes to confirm.")]
    prompt(client, "weather in Muscat")
    out = prompt(client, "note hi")
    assert [c["meta"] for c in calls(log)] == [{"caller": "alice"}]  # the write has not run yet
    client.post(f"/v1/actions/{out['pending_action']['id']}/confirm", headers={"X-API-Key": "k1"})
    assert [c["meta"] for c in calls(log)] == [{"caller": "alice"}, {"caller": "alice"}]


def test_curly_apostrophe_still_counts_as_unable(app_ctx):
    client, llm, log, _ = app_ctx
    llm.queue = [reply("I’m sorry, but I don’t have a tool for that."), tool_call("mcp__fake__get_weather", {"city": "X"}), reply("Sunny.")]
    assert prompt(client, "weather in X")["reply"] == "Sunny."


def test_pending_mcp_write_is_bullets_not_json(app_ctx):
    client, llm, log, _ = app_ctx
    llm.queue = [reply("I can't do that."), tool_call("mcp__fake__create_note", {"text": "call back", "data": {"title": "T"}}),
                 reply('{"status": "awaiting_user_confirmation"}')]
    text = prompt(client, "note it")["reply"]
    assert "{" not in text and "• text: call back" in text and "• title: T" in text and "yes" in text


def test_force_mcp_skips_phase_1_and_offers_only_mcp_tools(app_ctx):
    client, llm, log, _ = app_ctx
    llm.queue = [tool_call("mcp__fake__get_weather", {"city": "Muscat"}), reply("Sunny.")]
    r = client.post("/v1/prompt", headers={"X-API-Key": "k1"}, json={"message": "weather", "session_id": "s", "force_mcp": True})
    assert r.json()["reply"] == "Sunny."
    assert all(n.startswith("mcp__") for n in llm.offered[0]) and llm.offered[0]  # no built-in tool was offered
    assert [c["name"] for c in calls(log)] == ["get_weather"]


def test_force_mcp_without_mcp_configured_says_so_without_calling_the_llm(tmp_path):
    llm = RecordingLLM()
    settings = Settings(api_keys={"k1": "alice"}, audit_log_path=str(tmp_path / "a.log"), rate_limit_per_minute=1000)
    with TestClient(create_app(settings, by_caller(alice=StubERPNext()), llm)) as client:
        r = client.post("/v1/prompt", headers={"X-API-Key": "k1"}, json={"message": "x", "session_id": "s", "force_mcp": True})
    assert "not enabled" in r.json()["reply"] and llm.offered == []
