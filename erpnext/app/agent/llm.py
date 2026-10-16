"""Language model clients. The orchestrator speaks one dialect (OpenAI-style messages and tool calls);
each client here translates to its provider: any OpenAI-compatible endpoint, or Claude (Anthropic)."""
from __future__ import annotations

import json
import time
from typing import Any, Protocol

import requests

from app.config import Settings


class LLM(Protocol):
    def chat(self, messages: list[dict], tools: list[dict]) -> dict[str, Any]:
        """Return the assistant message dict: {'content': str, 'tool_calls': [{'id', 'function': {'name', 'arguments'}}]}."""


class OpenAICompatLLM:
    """OpenAI, Groq, Azure OpenAI, OpenRouter, Ollama... anything serving /chat/completions with tool calling."""

    def __init__(self, api_key: str, model: str, base_url: str = "", fallbacks: list[str] | None = None,
                 temperature: str = "0", max_tokens: int = 1200, session: requests.Session | None = None):
        self.key, self.model = api_key, model
        self.base = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.fallbacks = [m for m in (fallbacks or []) if m != model]
        self.temperature, self.max_tokens = temperature, max_tokens
        self.http = session or requests.Session()

    def _body(self, model: str, messages: list[dict], tools: list[dict]) -> dict:
        clean = [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]
        body: dict[str, Any] = {"model": model, "messages": clean, "tools": tools, "tool_choice": "auto"}
        # OpenAI's newer models want max_completion_tokens; most other servers still use max_tokens.
        body["max_completion_tokens" if "api.openai.com" in self.base else "max_tokens"] = self.max_tokens
        if self.temperature != "":
            body["temperature"] = float(self.temperature)
        return body

    def chat(self, messages, tools):
        errors = []
        for model in [self.model, *self.fallbacks]:
            for attempt in (1, 2):  # one retry: 429s and malformed tool calls are usually transient
                r = self.http.post(f"{self.base}/chat/completions", timeout=60,
                                   headers={"Authorization": f"Bearer {self.key}"}, json=self._body(model, messages, tools))
                if r.ok:
                    return r.json()["choices"][0]["message"]
                errors.append(f"{model}#{attempt}: {r.status_code} {r.text[:160]}")
                if r.status_code in (400, 401, 403, 404):
                    break  # retrying cannot help
                time.sleep(1.5)
        raise RuntimeError("LLM unavailable: " + " | ".join(errors[-4:]))


def to_anthropic_messages(messages: list[dict]) -> list[dict]:
    """OpenAI-style history -> Anthropic messages. System messages are passed separately."""
    out: list[dict] = []

    def push(role: str, blocks: list[dict]) -> None:
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)  # consecutive same-role turns are merged
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages:
        role = m["role"]
        if role == "user":
            if (m.get("content") or "").strip():
                push("user", [{"type": "text", "text": m["content"]}])
        elif role == "assistant":
            if m.get("_raw"):  # replay the model's own blocks unchanged (keeps thinking blocks valid)
                push("assistant", m["_raw"])
                continue
            blocks = []
            if (m.get("content") or "").strip():
                blocks.append({"type": "text", "text": m["content"]})
            for c in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": c["id"], "name": c["function"]["name"],
                               "input": json.loads(c["function"].get("arguments") or "{}")})
            if blocks:
                push("assistant", blocks)
        elif role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}])
    return out


class AnthropicLLM:
    """Claude through the official SDK."""

    def __init__(self, api_key: str, model: str, effort: str = "medium", max_tokens: int = 4096, client=None):
        if client is None:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key or None)
        self.client, self.model, self.effort, self.max_tokens = client, model, effort, max_tokens

    def chat(self, messages, tools):
        kwargs: dict[str, Any] = {
            "model": self.model, "max_tokens": self.max_tokens,
            "system": "\n\n".join(m["content"] for m in messages if m["role"] == "system"),
            "messages": to_anthropic_messages(messages),
            "tools": [{"name": t["function"]["name"], "description": t["function"]["description"],
                       "input_schema": t["function"]["parameters"]} for t in tools],
        }
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        try:
            resp = self.client.messages.create(**kwargs)
        except Exception as exc:  # SDK errors carry status and request id; keep the reason, not the stack
            raise RuntimeError(f"LLM unavailable: {type(exc).__name__}: {str(exc)[:200]}") from exc
        if resp.stop_reason == "refusal":
            return {"content": "I cannot help with that request.", "tool_calls": []}
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        calls = [{"id": b.id, "type": "function", "function": {"name": b.name, "arguments": json.dumps(b.input)}}
                 for b in resp.content if b.type == "tool_use"]
        return {"content": text, "tool_calls": calls, "_raw": [b.model_dump(exclude_none=True) for b in resp.content]}


def build_llm(settings: Settings) -> LLM:
    if settings.llm_provider == "anthropic":
        return AnthropicLLM(settings.llm_api_key, settings.llm_model, settings.llm_effort)
    if settings.llm_provider in ("openai", "groq", "openai-compatible"):
        return OpenAICompatLLM(settings.llm_api_key, settings.llm_model, settings.llm_base_url,
                               settings.llm_fallback_models, settings.llm_temperature)
    raise RuntimeError(f"Unknown LLM_PROVIDER '{settings.llm_provider}' (use 'anthropic' or 'openai')")
