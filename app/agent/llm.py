from __future__ import annotations

import time
from typing import Any, Protocol

import requests


class LLM(Protocol):
    def chat(self, messages: list[dict], tools: list[dict]) -> dict[str, Any]:
        """Return the assistant message dict (may contain tool_calls)."""


class GroqLLM:
    base = "https://api.groq.com/openai/v1"
    FALLBACKS = ("openai/gpt-oss-120b", "qwen/qwen3.8-27b")  # models visible to the key; configured model goes first

    def __init__(self, api_key: str, model: str):
        self.key, self.model = api_key, model
        self.fallbacks = [m for m in self.FALLBACKS if m != model]

    def chat(self, messages, tools):
        errors = []
        for model in [self.model, *self.fallbacks]:
            for attempt in (1, 2):  # one retry: 429s and malformed tool calls are usually transient
                r = requests.post(f"{self.base}/chat/completions", timeout=60,
                                  headers={"Authorization": f"Bearer {self.key}"},
                                  json={"model": model, "messages": messages, "tools": tools,
                                        "tool_choice": "auto", "temperature": 0, "max_tokens": 1200})
                if r.ok:
                    return r.json()["choices"][0]["message"]
                errors.append(f"{model}#{attempt}: {r.status_code} {r.text[:160]}")
                if r.status_code in (404, 401, 403):
                    break  # retrying cannot help
                time.sleep(1.5)
        raise RuntimeError("LLM unavailable: " + " | ".join(errors[-4:]))
