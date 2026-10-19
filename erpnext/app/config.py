from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from app.agent.mcp import parse_mcp_servers

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _pairs(raw: str) -> dict[str, str]:
    out = {}
    for pair in raw.split(","):
        if ":" in pair:
            k, v = pair.split(":", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


@dataclass
class Settings:
    erp_url: str = "http://127.0.0.1:8000"
    erp_timeout: float = 20.0
    # caller -> (api_key, api_secret): every caller acts as its own ERPNext user
    erp_users: dict[str, tuple[str, str]] = field(default_factory=dict)
    # "key:caller" -> {key: caller}
    api_keys: dict[str, str] = field(default_factory=dict)
    # callers allowed to submit (finalize) Sales Orders; others may only draft
    confirm_callers: set[str] = field(default_factory=set)
    # callers allowed to create new Items (and their selling price), alone or inline in a quotation/order
    item_creators: set[str] = field(default_factory=set)
    rate_limit_per_minute: int = 60
    max_page_size: int = 50
    action_ttl_seconds: int = 900
    audit_log_path: str = str(ROOT / "audit.log")
    timezone: str = "Asia/Muscat"
    session_ttl_seconds: int = 3600
    session_max_messages: int = 12
    llm_provider: str = "anthropic"          # anthropic | openai (any OpenAI-compatible endpoint)
    llm_api_key: str = ""
    llm_model: str = "claude-sonnet-5-5"
    llm_base_url: str = ""
    llm_fallback_models: list[str] = field(default_factory=list)
    llm_temperature: str = "0"
    llm_effort: str = "medium"
    # MCP fallback servers (name -> command); empty = the built-in tools are the only capability
    mcp_servers: dict[str, str] = field(default_factory=dict)
    mcp_timeout: float = 60.0

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        env = os.environ.get
        users = {}
        for triple in env("ERP_USERS", "").split(","):
            parts = triple.strip().split(":")
            if len(parts) == 3 and all(parts):
                users[parts[0]] = (parts[1], parts[2])
        return cls(
            erp_url=env("ERP_URL", "http://127.0.0.1:8000").rstrip("/"),
            erp_timeout=float(env("ERP_TIMEOUT", "20")),
            erp_users=users,
            api_keys=_pairs(env("API_KEYS", "")),
            confirm_callers={c.strip() for c in env("CONFIRM_CALLERS", "").split(",") if c.strip()},
            item_creators={c.strip() for c in env("ITEM_CREATORS", "").split(",") if c.strip()},
            rate_limit_per_minute=int(env("RATE_LIMIT_PER_MINUTE", "60")),
            max_page_size=int(env("MAX_PAGE_SIZE", "50")),
            action_ttl_seconds=int(env("ACTION_TTL_SECONDS", "900")),
            timezone=env("TIMEZONE", "Asia/Muscat"),
            session_ttl_seconds=int(env("SESSION_TTL_SECONDS", "3600")),
            session_max_messages=int(env("SESSION_MAX_MESSAGES", "12")),
            llm_provider=env("LLM_PROVIDER", "anthropic").lower(),
            llm_api_key=env("LLM_API_KEY", ""),
            llm_model=env("LLM_MODEL", "claude-sonnet-5-5"),
            llm_base_url=env("LLM_BASE_URL", "").rstrip("/"),
            llm_fallback_models=[m.strip() for m in env("LLM_FALLBACK_MODELS", "").split(",") if m.strip()],
            llm_temperature=env("LLM_TEMPERATURE", "0"),
            llm_effort=env("LLM_EFFORT", "medium"),
            mcp_servers=parse_mcp_servers(env("MCP_SERVERS", "")),
            mcp_timeout=float(env("MCP_TIMEOUT", "60")),
        )
