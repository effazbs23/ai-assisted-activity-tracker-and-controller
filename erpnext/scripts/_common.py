"""Shared helpers for the ERPNext setup and seed scripts."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_dotenv  # noqa: E402

load_dotenv()

ERP_URL = os.environ.get("ERP_URL", "http://127.0.0.1:8000").rstrip("/")


class AdminSession:
    """Administrator session (cookie login). Used only for one-off setup and seeding."""

    def __init__(self):
        self.http = requests.Session()
        r = self.http.post(f"{ERP_URL}/api/method/login", timeout=30, json={
            "usr": os.environ.get("ERP_ADMIN_USERNAME", "Administrator"), "pwd": os.environ.get("ERP_ADMIN_PASSWORD", "admin")})
        if not r.ok:
            sys.exit(f"Cannot log in to ERPNext at {ERP_URL} as admin: {r.status_code} {r.text[:200]}")

    def request(self, method: str, path: str, **kw):
        r = self.http.request(method, f"{ERP_URL}{path}", timeout=60, **kw)
        if not r.ok:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
        return r.json()

    def exists(self, doctype: str, name: str) -> bool:
        return self.http.get(f"{ERP_URL}/api/resource/{doctype}/{name}", timeout=30).status_code == 200

    def insert(self, doctype: str, doc: dict) -> dict:
        return self.request("POST", f"/api/resource/{doctype}", json=doc)["data"]


def update_env(values: dict[str, str], path: Path = ROOT / ".env") -> None:
    """Set KEY=VALUE lines in .env (creating it from .env.example), keeping everything else."""
    if not path.exists():
        path.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    lines, seen = path.read_text(encoding="utf-8").splitlines(), set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in values and "=" in line and not line.lstrip().startswith("#"):
            lines[i], _ = f"{key}={values[key]}", seen.add(key)
    lines += [f"{k}={v}" for k, v in values.items() if k not in seen]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
