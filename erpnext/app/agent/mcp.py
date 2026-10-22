"""MCP (Model Context Protocol) fallback client.

The built-in tool set in tools.py stays the main controller: a small, validated set of
ERPNext operations behind the propose -> yes confirmation gate. Requests it cannot fulfil
fall back to external MCP servers configured with MCP_SERVERS; their tools become
available to the LLM as mcp__<server>__<tool>.

This is a minimal stdio client: JSON-RPC 2.0 with newline-delimited messages, implementing
only initialize, tools/list and tools/call. Tools annotated readOnlyHint (or whose name
looks like a read) run immediately; every other MCP call is parked as a pending action
and still needs the user's yes, like every other write in this app.
"""
from __future__ import annotations

import json
import os
import re
import selectors
import shlex
import subprocess
import threading
import time
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "erpnext-activity-assistant", "version": "0.1.0"}
# Used only when a tool has no readOnlyHint annotation. Underscore-terminated so e.g. "checkout" or "showcase_create"
# is never mistaken for a read. Anything not matched is treated as a write (needs the user's yes).
READ_ONLY_PREFIXES = ("get_", "list_", "search_", "read_", "query_", "find_", "count_", "check_", "is_", "has_",
                      "fetch_", "describe_", "lookup_", "show_")
MAX_CONTENT_CHARS = 4000


class MCPError(Exception):
    """An MCP server refused, failed or timed out."""


class MCPServer:
    """One MCP server running as a child process, spoken to over stdin/stdout."""

    def __init__(self, name: str, command: str, timeout: float = 60.0):
        self.name, self.timeout = name, timeout
        self.command = shlex.split(command)
        self.proc: subprocess.Popen | None = None
        self._id = 0
        self._buf = b""
        self._lock = threading.Lock()

    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        if not self.command:
            raise MCPError(f"{self.name}: empty command")
        try:
            self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, bufsize=0)
        except OSError as exc:
            raise MCPError(f"{self.name}: cannot start '{' '.join(self.command)}' ({exc})") from exc
        self._buf = b""
        result = self._request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                              "clientInfo": CLIENT_INFO})
        if not isinstance(result, dict) or "protocolVersion" not in result:
            raise MCPError(f"{self.name}: bad initialize response")
        self._notify("notifications/initialized")

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def _notify(self, method: str) -> None:
        self._write({"jsonrpc": "2.0", "method": method})

    def _write(self, payload: dict) -> None:
        if not self.proc or not self.proc.stdin:
            raise MCPError(f"{self.name}: not connected")
        try:
            self.proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise MCPError(f"{self.name}: cannot send ({exc})") from exc

    def _read_message(self, deadline: float) -> dict:
        """Next JSON-RPC message from the server (notifications included). Binary reads,
        so select() and an internal buffer never disagree."""
        sel = selectors.DefaultSelector()
        assert self.proc and self.proc.stdout
        sel.register(self.proc.stdout, selectors.EVENT_READ)
        try:
            while True:
                if b"\n" in self._buf:
                    line, self._buf = self._buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except ValueError as exc:
                        raise MCPError(f"{self.name}: malformed response ({exc})") from exc
                    if isinstance(msg, dict):
                        return msg
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MCPError(f"{self.name}: timed out")
                sel.select(min(remaining, 0.5))
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    raise MCPError(f"{self.name}: server exited ({' '.join(self.command)})")
                self._buf += chunk
        finally:
            sel.close()

    def _request(self, method: str, params: dict) -> Any:
        with self._lock:
            self._id += 1
            rid = self._id
            self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            deadline = time.monotonic() + self.timeout
            while True:
                msg = self._read_message(deadline)
                if msg.get("id") != rid:
                    continue  # a notification or an unrelated response
                if "error" in msg:
                    err = msg["error"] or {}
                    raise MCPError(f"{self.name}: {err.get('message', 'error')} (code {err.get('code')})")
                return msg.get("result")

    def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        while True:
            result = self._request("tools/list", {"cursor": cursor} if cursor else {}) or {}
            tools.extend(t for t in (result.get("tools") or []) if isinstance(t, dict) and t.get("name"))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict | None = None, caller: str | None = None) -> str:
        params: dict = {"name": name, "arguments": arguments or {}}
        if caller:
            params["_meta"] = {"caller": caller}  # servers that act per user (mcp_server/) read this; others ignore it
        result = self._request("tools/call", params) or {}
        parts = []
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, dict):
                parts.append(f"[{block.get('type', 'content')} omitted]")
        text = "\n".join(p for p in parts if p) or "(no content)"
        if result.get("isError"):
            raise MCPError(text[:500])
        return text[:MAX_CONTENT_CHARS]


def parse_mcp_servers(raw: str) -> dict[str, str]:
    """'name=command args; other=cmd' -> {name: command}. Names may not contain '__'."""
    out: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, command = part.split("=", 1)
        name, command = name.strip(), command.strip()
        if name and command and "__" not in name:
            out[name] = command
    return out


_TOOL_NAME = re.compile(r"[^a-zA-Z0-9_-]")
MAX_TOOL_NAME = 64  # Anthropic and OpenAI both cap tool names at 64 chars of [a-zA-Z0-9_-]


def _read_only(raw: dict) -> bool:
    """MCP annotations win; destructiveHint=true is always a write; otherwise fall back to the name."""
    ann = raw.get("annotations") or {}
    if ann.get("destructiveHint") is True:
        return False
    if "readOnlyHint" in ann:
        return bool(ann["readOnlyHint"])
    return raw["name"].lower().startswith(READ_ONLY_PREFIXES)


class MCPHub:
    """Every configured MCP server, presented to the LLM as mcp__<server>__<tool>.

    Thread-safe: FastAPI runs sync endpoints in a thread pool, so discovery and calls are serialised."""

    def __init__(self, servers: dict[str, str], timeout: float = 60.0, retry_after: float = 60.0):
        self._conns = {name: MCPServer(name, command, timeout) for name, command in servers.items()}
        self.retry_after = retry_after
        self._tools: dict[str, dict] = {}          # full name -> server, tool, schema, read_only
        self._loaded: set[str] = set()             # servers whose tool list is cached
        self._broken_until: dict[str, float] = {}  # server -> do not respawn before this clock
        self._lock = threading.RLock()

    @property
    def names(self) -> list[str]:
        return sorted(self._conns)

    def _discover(self, name: str, conn: MCPServer) -> None:
        conn.start()
        found = {}
        for raw in conn.list_tools():
            bare = raw["name"]
            full = _TOOL_NAME.sub("_", f"mcp__{name}__{bare}")
            if len(full) > MAX_TOOL_NAME or full in found:
                continue  # the provider would reject the whole request; skip just this tool
            read_only = _read_only(raw)
            found[full] = {
                "name": full, "server": name, "tool": bare,
                "description": (f"[MCP:{name}] {raw.get('description') or bare} "
                                + ("(Read-only: runs immediately.)" if read_only
                                   else "(Changes data: only runs after the user replies yes.)"))[:1000],
                "input_schema": raw.get("inputSchema") or {"type": "object", "properties": {}},
                "read_only": read_only,
            }
        self._tools.update(found)
        self._loaded.add(name)

    def tools(self) -> list[dict]:
        """OpenAI-style schemas for every tool of every reachable server (cached per server)."""
        with self._lock:
            for name, conn in self._conns.items():
                if name in self._loaded or time.monotonic() < self._broken_until.get(name, 0):
                    continue
                try:
                    self._discover(name, conn)
                except MCPError:
                    conn.stop()
                    self._broken_until[name] = time.monotonic() + self.retry_after
            return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                      "parameters": t["input_schema"]}}
                    for t in self._tools.values()]

    def describe(self, full_name: str) -> dict | None:
        self.tools()
        meta = self._tools.get(full_name)
        return {k: meta[k] for k in ("server", "tool", "read_only")} if meta else None

    def is_read_only(self, full_name: str) -> bool:
        return bool(self._tools.get(full_name, {}).get("read_only"))

    def call(self, full_name: str, arguments: dict | None = None, caller: str | None = None) -> dict:
        self.tools()
        with self._lock:
            meta = self._tools.get(full_name)
            if not meta:
                raise MCPError(f"unknown MCP tool '{full_name}'")
            conn = self._conns[meta["server"]]
            conn.start()  # respawns a server that exited since discovery
            return {"server": meta["server"], "tool": meta["tool"], "content": conn.call_tool(meta["tool"], arguments, caller)}

    def close(self) -> None:
        with self._lock:
            for conn in self._conns.values():
                conn.stop()
