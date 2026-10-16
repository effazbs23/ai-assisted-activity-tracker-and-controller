"""Thin ERPNext (Frappe) REST adapter. Services call this; nothing else talks to ERPNext.

One client per caller: it authenticates with that caller's own API key/secret, so ERPNext's
roles and permissions decide what the caller can read and change.
"""
from __future__ import annotations

import json
from typing import Any, Protocol

import requests

from app.config import Settings


class ERPError(Exception):
    """ERPNext rejected the call or was unreachable."""


class ERPAPI(Protocol):
    user: str

    def get_list(self, doctype: str, filters: list | None = None, fields: list[str] | None = None, limit: int = 10,
                 order_by: str | None = None, or_filters: list | None = None) -> list[dict[str, Any]]: ...
    def get_doc(self, doctype: str, name: str) -> dict[str, Any]: ...
    def insert(self, doctype: str, doc: dict[str, Any]) -> dict[str, Any]: ...
    def update(self, doctype: str, name: str, values: dict[str, Any]) -> dict[str, Any]: ...
    def submit(self, doctype: str, name: str) -> dict[str, Any]: ...
    def call(self, method: str, **kwargs) -> Any: ...


def _message(resp: requests.Response) -> str:
    """Best human-readable reason from a Frappe error response."""
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    if body.get("_server_messages"):
        try:
            msgs = [json.loads(m).get("message", "") for m in json.loads(body["_server_messages"])]
            text = " ".join(m for m in msgs if m)
            if text:
                return _strip_html(text)
        except (ValueError, AttributeError):
            pass
    return _strip_html(str(body.get("exception") or body.get("message") or f"HTTP {resp.status_code}"))[:300]


def _strip_html(text: str) -> str:
    import re
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


class ERPClient:
    def __init__(self, base_url: str, api_key: str, api_secret: str, timeout: float = 20.0,
                 session: requests.Session | None = None, user: str = ""):
        self.base, self.timeout = base_url.rstrip("/"), timeout
        self.http = session or requests.Session()
        self.http.headers["Authorization"] = f"token {api_key}:{api_secret}"
        self.user = user

    def _request(self, method: str, path: str, **kw) -> Any:
        try:
            r = self.http.request(method, f"{self.base}{path}", timeout=self.timeout, **kw)
        except requests.RequestException as exc:
            raise ERPError(f"ERPNext unreachable: {exc}") from exc
        if r.status_code == 404:
            raise ERPError("ERPNext record or method not found")
        if r.status_code in (401, 403):
            raise ERPError(f"ERPNext denied access: {_message(r)}")
        if not r.ok:
            raise ERPError(_message(r))
        try:
            return r.json()
        except ValueError as exc:
            raise ERPError("ERPNext returned a non-JSON response") from exc

    def whoami(self) -> str:
        self.user = self._request("GET", "/api/method/frappe.auth.get_logged_user")["message"]
        return self.user

    def get_list(self, doctype, filters=None, fields=None, limit=10, order_by=None, or_filters=None):
        params: dict[str, Any] = {"limit_page_length": limit, "fields": json.dumps(fields or ["name"])}
        if filters:
            params["filters"] = json.dumps(filters)
        if or_filters:
            params["or_filters"] = json.dumps(or_filters)
        if order_by:
            params["order_by"] = order_by
        return self._request("GET", f"/api/resource/{doctype}", params=params)["data"]

    def get_doc(self, doctype, name):
        return self._request("GET", f"/api/resource/{doctype}/{name}")["data"]

    def insert(self, doctype, doc):
        return self._request("POST", f"/api/resource/{doctype}", json=doc)["data"]

    def update(self, doctype, name, values):
        return self._request("PUT", f"/api/resource/{doctype}/{name}", json=values)["data"]

    def submit(self, doctype, name):
        doc = self.get_doc(doctype, name)
        return self._request("POST", "/api/method/frappe.client.submit", json={"doc": doc})["message"]

    def call(self, method, **kwargs):
        return self._request("POST", f"/api/method/{method}", json=kwargs)["message"]


def client_factory(settings: Settings):
    """caller -> ERPClient bound to that caller's own ERPNext credentials."""
    cache: dict[str, ERPClient] = {}

    def for_caller(caller: str) -> ERPClient:
        if caller not in cache:
            creds = settings.erp_users.get(caller)
            if not creds:
                raise ERPError(f"No ERPNext user is configured for caller '{caller}'")
            cache[caller] = ERPClient(settings.erp_url, creds[0], creds[1], settings.erp_timeout)
        return cache[caller]

    return for_caller
