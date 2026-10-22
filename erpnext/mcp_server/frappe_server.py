"""stdio MCP server: any ERPNext DocType, executed as the calling user.

Run from erpnext/:  python -m mcp_server.frappe_server   (MCP_SERVERS="frappe=venv/bin/python -m mcp_server.frappe_server")
The app sends the caller's name in tools/call params._meta.caller; the call then uses that caller's own ERPNext
API key (ERP_USERS), so ERPNext roles apply. No caller = refused. There is deliberately no delete tool and no
arbitrary-method tool. Reads carry readOnlyHint; the app parks every other call until the user says yes.
"""
from __future__ import annotations

import json
import sys

from app.config import Settings
from app.erp.client import ERPError, client_factory

# Security-critical DocTypes: never writable through this server, and not readable either, except READABLE.
DENIED = {"User", "Role", "Has Role", "DocPerm", "Custom DocPerm", "User Permission", "API Key", "OAuth Client",
          "OAuth Bearer Token", "OAuth Authorization Code", "Token Cache", "Social Login Key", "System Settings",
          "Email Account", "Session Default Settings", "Access Log", "Activity Log", "Error Log"}

READABLE = {"User"}  # read-only; ERPNext still applies the caller's own permissions. Passwords/secrets are masked by Frappe.

_STR = {"type": "string"}
_OBJ = {"type": "object"}


def _tool(name, description, props, required, read_only, destructive=False):
    ann = {"readOnlyHint": read_only}
    if destructive:
        ann["destructiveHint"] = True
    return {"name": name, "description": description, "annotations": ann,
            "inputSchema": {"type": "object", "properties": props, "required": required}}


_DT = {"doctype": {**_STR, "description": "ERPNext DocType, e.g. Warehouse, Bin, Employee, Project"}}
TOOLS = [
    _tool("list_docs", "List records of a DocType (default field: name). filters: [[field, op, value], ...] or {field: value}. Use only real field names; call describe_doctype if unsure.",
          {**_DT, "filters": {}, "fields": {"type": "array", "items": _STR}, "limit": {"type": "integer"}, "order_by": _STR},
          ["doctype"], True),
    _tool("get_doc", "Read one full record.", {**_DT, "name": _STR}, ["doctype", "name"], True),
    _tool("count_docs", "Count records of a DocType matching optional filters.", {**_DT, "filters": {}}, ["doctype"], True),
    _tool("describe_doctype", "Fields, required flags, select options and link targets of a DocType. Call before create_doc.",
          _DT, ["doctype"], True),
    _tool("run_report", "Run a saved ERPNext report (e.g. Stock Balance, General Ledger).",
          {"report_name": _STR, "filters": _OBJ}, ["report_name"], True),
    _tool("create_doc", "Create a draft record. data: field values incl. child tables.",
          {**_DT, "data": _OBJ}, ["doctype", "data"], False),
    _tool("update_doc", "Change fields of an existing record.",
          {**_DT, "name": _STR, "data": _OBJ}, ["doctype", "name", "data"], False),
    _tool("submit_doc", "Submit a draft record.", {**_DT, "name": _STR}, ["doctype", "name"], False),
    _tool("cancel_doc", "Cancel a submitted record.", {**_DT, "name": _STR}, ["doctype", "name"], False, destructive=True),
]


class Server:
    def __init__(self, settings: Settings, erp_for=None):
        self.settings = settings
        self.erp_for = erp_for or client_factory(settings)

    def _check(self, caller: str, doctype: str, op: str) -> None:
        s = self.settings
        if not isinstance(doctype, str) or not doctype or "/" in doctype:
            raise ValueError("doctype is required")
        if doctype in DENIED and not (op == "read" and doctype in READABLE):
            raise PermissionError(f"{doctype} is not available through this assistant")
        if doctype == "Sales Order" and op in ("submit", "cancel") and caller not in s.confirm_callers:
            raise PermissionError("Only authorised callers can submit or cancel Sales Orders")
        if doctype == "Item" and op == "create" and caller not in s.item_creators:
            raise PermissionError("Only authorised callers can create items")

    def call(self, name: str, a: dict, caller: str):
        if not caller:
            raise PermissionError("No caller identity was supplied")
        erp = self.erp_for(caller)  # the caller's own ERPNext user
        dt = a.get("doctype")
        if name == "run_report":
            return erp.call("frappe.desk.query_report.run", report_name=a["report_name"], filters=a.get("filters") or {})
        op = {"create_doc": "create", "update_doc": "update", "submit_doc": "submit", "cancel_doc": "cancel"}.get(name, "read")
        self._check(caller, dt, op)
        if name == "list_docs":
            limit = max(1, min(int(a.get("limit") or 20), self.settings.max_page_size))
            return erp.get_list(dt, a.get("filters"), a.get("fields"), limit, a.get("order_by"))
        if name == "count_docs":
            return erp.call("frappe.client.get_count", doctype=dt, filters=a.get("filters") or {})
        if name == "get_doc":
            return erp.get_doc(dt, a["name"])
        if name == "describe_doctype":
            meta = erp.doctype_meta(dt)
            keep = ("fieldname", "label", "fieldtype", "reqd", "options")
            return {"name": dt, "is_submittable": meta.get("is_submittable"),
                    "fields": [{k: f.get(k) for k in keep if f.get(k)} for f in meta.get("fields", [])
                               if f.get("fieldtype") not in ("Section Break", "Column Break", "Tab Break")]}
        if name == "create_doc":
            return erp.insert(dt, a["data"])
        if name == "update_doc":
            return erp.update(dt, a["name"], a["data"])
        if name == "submit_doc":
            return erp.submit(dt, a["name"])
        if name == "cancel_doc":
            return erp.call("frappe.client.cancel", doctype=dt, name=a["name"])
        raise ValueError(f"unknown tool {name}")

    def handle(self, req: dict) -> dict | None:
        method, rid, params = req.get("method"), req.get("id"), req.get("params") or {}
        if rid is None:
            return None  # notification
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "frappe", "version": "0.1.0"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            caller = (params.get("_meta") or {}).get("caller", "")
            try:
                out, err = json.dumps(self.call(params.get("name", ""), params.get("arguments") or {}, caller),
                                      default=str), False
            except (ERPError, PermissionError, ValueError, KeyError, TypeError) as exc:
                out, err = f"{type(exc).__name__}: {exc}", True
            result = {"content": [{"type": "text", "text": out}], "isError": err}
        else:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}


def main() -> None:
    server = Server(Settings.from_env())
    for line in sys.stdin:
        if not line.strip():
            continue
        resp = server.handle(json.loads(line))
        if resp:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
