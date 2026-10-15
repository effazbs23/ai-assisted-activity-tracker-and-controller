"""Thin Odoo JSON-RPC adapter. Services call this; nothing else talks to Odoo."""
from __future__ import annotations

import itertools
from typing import Any, Protocol

import requests

from app.config import Settings


class OdooError(Exception):
    """Odoo rejected the call or was unreachable."""


class OdooAPI(Protocol):
    def search_read(self, model: str, domain: list, fields: list[str], limit: int = 10, order: str | None = None) -> list[dict[str, Any]]: ...
    def create(self, model: str, values: dict[str, Any]) -> int: ...
    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool: ...
    def call(self, model: str, method: str, ids: list[int], **kwargs) -> Any: ...


class OdooClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.s = settings
        self.http = session or requests.Session()
        self.uid: int | None = None
        self._ids = itertools.count(1)

    def _rpc(self, service: str, method: str, *args) -> Any:
        payload = {"jsonrpc": "2.0", "method": "call", "id": next(self._ids),
                   "params": {"service": service, "method": method, "args": list(args)}}
        try:
            r = self.http.post(f"{self.s.odoo_url}/jsonrpc", json=payload, timeout=self.s.odoo_timeout)
            r.raise_for_status()
            body = r.json()
        except (requests.RequestException, ValueError) as exc:
            raise OdooError(f"Odoo unreachable: {exc}") from exc
        if body.get("error"):
            data = body["error"].get("data", {})
            raise OdooError(data.get("message") or body["error"].get("message", "Odoo error"))
        return body.get("result")

    def authenticate(self) -> int:
        uid = self._rpc("common", "authenticate", self.s.odoo_db, self.s.odoo_username, self.s.odoo_password, {})
        if not uid:
            raise OdooError("Odoo authentication failed")
        self.uid = int(uid)
        return self.uid

    def _execute(self, model: str, method: str, args: list, kwargs: dict | None = None) -> Any:
        if self.uid is None:
            self.authenticate()
        return self._rpc("object", "execute_kw", self.s.odoo_db, self.uid, self.s.odoo_password,
                         model, method, args, kwargs or {})

    def search_read(self, model, domain, fields, limit=10, order=None):
        kw: dict[str, Any] = {"fields": fields, "limit": limit}
        if order:
            kw["order"] = order
        return self._execute(model, "search_read", [domain], kw)

    def create(self, model, values):
        return int(self._execute(model, "create", [values]))

    def write(self, model, ids, values):
        return bool(self._execute(model, "write", [ids, values]))

    def call(self, model, method, ids, **kwargs):
        return self._execute(model, method, [ids], kwargs)
