from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

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


@dataclass
class Settings:
    odoo_url: str = "http://127.0.0.1:8069"
    odoo_db: str = ""
    odoo_username: str = ""
    odoo_password: str = ""
    odoo_timeout: float = 20.0
    # "key:caller,key2:caller2" -> {key: caller}
    api_keys: dict[str, str] = field(default_factory=dict)
    # callers allowed to confirm orders (others may only draft)
    confirm_callers: set[str] = field(default_factory=set)
    rate_limit_per_minute: int = 60
    max_page_size: int = 50
    action_ttl_seconds: int = 900
    audit_log_path: str = str(ROOT / "audit.log")
    timezone: str = "Asia/Dhaka"
    work_start_hour: int = 9
    work_end_hour: int = 18
    groq_key: str = ""
    groq_chat_model: str = "openai/gpt-oss-20b"
    session_ttl_seconds: int = 3600
    session_max_messages: int = 12

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        keys: dict[str, str] = {}
        for pair in os.environ.get("API_KEYS", "").split(","):
            if ":" in pair:
                k, caller = pair.split(":", 1)
                if k.strip() and caller.strip():
                    keys[k.strip()] = caller.strip()
        return cls(
            odoo_url=os.environ.get("ODOO_URL", "http://127.0.0.1:8069").rstrip("/"),
            odoo_db=os.environ.get("ODOO_DB", ""),
            odoo_username=os.environ.get("ODOO_USERNAME", ""),
            odoo_password=os.environ.get("ODOO_PASSWORD", ""),
            odoo_timeout=float(os.environ.get("ODOO_TIMEOUT", "20")),
            api_keys=keys,
            confirm_callers={c.strip() for c in os.environ.get("CONFIRM_CALLERS", "").split(",") if c.strip()},
            rate_limit_per_minute=int(os.environ.get("RATE_LIMIT_PER_MINUTE", "60")),
            max_page_size=int(os.environ.get("MAX_PAGE_SIZE", "50")),
            action_ttl_seconds=int(os.environ.get("ACTION_TTL_SECONDS", "900")),
            timezone=os.environ.get("TIMEZONE", "Asia/Dhaka"),
            work_start_hour=int(os.environ.get("WORK_START_HOUR", "9")),
            work_end_hour=int(os.environ.get("WORK_END_HOUR", "18")),
            groq_key=os.environ.get("GROQ_API_KEY", ""),
            groq_chat_model=os.environ.get("GROQ_CHAT_MODEL", "openai/gpt-oss-20b"),
            session_ttl_seconds=int(os.environ.get("SESSION_TTL_SECONDS", "3600")),
            session_max_messages=int(os.environ.get("SESSION_MAX_MESSAGES", "12")),
        )
