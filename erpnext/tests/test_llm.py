import json
from types import SimpleNamespace as NS

import pytest

from app.agent.llm import AnthropicLLM, OpenAICompatLLM, build_llm, to_anthropic_messages
from app.agent.tools import TOOLS
from app.config import Settings


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.text = status, body or {}, json.dumps(body or {})
        self.ok = status < 400

    def json(self):
        return self._body


class FakeHTTP:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def post(self, url, headers=None, json=None, timeout=None):
        self.requests.append((url, headers, json))
        return self.responses.pop(0)


OK = Resp(200, {"choices": [{"message": {"content": "hi", "tool_calls": None}}]})


def test_openai_compatible_request_shape():
    http = FakeHTTP(OK)
    llm = OpenAICompatLLM("key", "gpt-x", "", session=http)
    assert llm.chat([{"role": "system", "content": "s"}, {"role": "assistant", "content": "a", "_raw": [1]}], TOOLS)["content"] == "hi"
    url, headers, body = http.requests[0]
    assert url == "https://api.openai.com/v1/chat/completions" and headers["Authorization"] == "Bearer key"
    assert body["max_completion_tokens"] == 1200 and "max_tokens" not in body and body["temperature"] == 0.0
    assert all("_raw" not in m for m in body["messages"])  # provider-specific fields never leak to other providers


def test_openai_compatible_other_server_and_temperature_omitted():
    http = FakeHTTP(OK)
    OpenAICompatLLM("k", "m", "https://api.groq.com/openai/v1/", temperature="", session=http).chat([], TOOLS)
    url, _, body = http.requests[0]
    assert url == "https://api.groq.com/openai/v1/chat/completions" and "max_tokens" in body and "temperature" not in body


def test_openai_compatible_falls_back_to_next_model():
    http = FakeHTTP(Resp(400, {"error": "bad model"}), OK)
    llm = OpenAICompatLLM("k", "main", fallbacks=["backup", "main"], session=http)
    assert llm.chat([], TOOLS)["content"] == "hi"
    assert [r[2]["model"] for r in http.requests] == ["main", "backup"]


def test_openai_compatible_all_failing_raises(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    http = FakeHTTP(Resp(429), Resp(429))
    with pytest.raises(RuntimeError, match="LLM unavailable"):
        OpenAICompatLLM("k", "m", session=http).chat([], TOOLS)


def test_message_conversion_to_anthropic_merges_and_orders_tool_results():
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "find xyz"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "function": {"name": "search_clients", "arguments": '{"query": "xyz"}'}},
                {"id": "t2", "function": {"name": "search_items", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "[1]"},
            {"role": "tool", "tool_call_id": "t2", "content": "[2]"},
            {"role": "assistant", "content": "   "},          # empty turns are dropped
            {"role": "user", "content": "thanks"}]
    out = to_anthropic_messages(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert out[1]["content"][0] == {"type": "tool_use", "id": "t1", "name": "search_clients", "input": {"query": "xyz"}}
    assert [b["type"] for b in out[2]["content"]] == ["tool_result", "tool_result", "text"]  # results first, then the new text


def test_raw_blocks_are_replayed_unchanged():
    raw = [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": "hello"}]
    out = to_anthropic_messages([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello", "_raw": raw}])
    assert out[1]["content"] == raw


class Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self, exclude_none=True):
        return dict(self.__dict__)


class FakeAnthropic:
    def __init__(self, content, stop="end_turn"):
        self.calls = []
        self.messages = self
        self._resp = NS(content=content, stop_reason=stop)

    def create(self, **kw):
        self.calls.append(kw)
        return self._resp


def test_anthropic_request_and_tool_call_parsing():
    client = FakeAnthropic([Block(type="text", text="Looking up."), Block(type="tool_use", id="tu1", name="search_clients", input={"query": "xyz"})],
                           stop="tool_use")
    llm = AnthropicLLM("k", "claude-sonnet-5-5", effort="low", client=client)
    out = llm.chat([{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}], TOOLS)
    kw = client.calls[0]
    assert kw["model"] == "claude-sonnet-5-5" and kw["system"] == "SYS" and kw["output_config"] == {"effort": "low"}
    assert {"temperature", "thinking", "tool_choice"}.isdisjoint(kw)  # rejected or unnecessary on current Claude models
    assert kw["tools"][0]["name"] == "search_clients" and kw["tools"][0]["input_schema"]["type"] == "object"
    assert out["content"] == "Looking up."
    assert out["tool_calls"] == [{"id": "tu1", "type": "function", "function": {"name": "search_clients", "arguments": '{"query": "xyz"}'}}]
    assert out["_raw"][1]["type"] == "tool_use"


def test_anthropic_refusal_and_no_effort():
    client = FakeAnthropic([], stop="refusal")
    out = AnthropicLLM("k", "claude-haiku-4-5", effort="", client=client).chat([{"role": "user", "content": "x"}], TOOLS)
    assert "cannot help" in out["content"] and out["tool_calls"] == [] and "output_config" not in client.calls[0]


def test_anthropic_api_errors_become_runtime_errors():
    class Boom:
        messages = None

        def __init__(self):
            self.messages = self

        def create(self, **kw):
            raise ValueError("rate limited")
    with pytest.raises(RuntimeError, match="LLM unavailable"):
        AnthropicLLM("k", "m", client=Boom()).chat([{"role": "user", "content": "x"}], TOOLS)


def test_provider_selection(monkeypatch):
    monkeypatch.setattr("anthropic.Anthropic", lambda api_key=None: object())
    assert isinstance(build_llm(Settings(llm_provider="anthropic")), AnthropicLLM)
    assert isinstance(build_llm(Settings(llm_provider="openai", llm_base_url="https://api.groq.com/openai/v1")), OpenAICompatLLM)
    assert isinstance(build_llm(Settings(llm_provider="groq")), OpenAICompatLLM)
    with pytest.raises(RuntimeError):
        build_llm(Settings(llm_provider="mystery"))
