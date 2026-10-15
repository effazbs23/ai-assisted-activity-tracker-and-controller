"""Tool schema exposed to the LLM, mapped 1:1 to domain services. Writes only create pending actions."""
from __future__ import annotations

import datetime as dt
from typing import Any

from app.services import calendar, crm, proposals, sales
from app.services.errors import NotFound, ValidationFailed


def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


S, I, N = {"type": "string"}, {"type": "integer"}, {"type": "number"}

TOOLS = [
    _fn("search_customers", "Find customers by name. Returns ids.", {"query": S}, ["query"]),
    _fn("search_products", "Find sellable products by name. Returns ids and list prices.", {"query": S}, ["query"]),
    _fn("search_leads", "Find CRM leads/opportunities by name (empty query = latest).", {"query": S}, []),
    _fn("list_quotations", "List draft/sent quotations, optionally filtered by customer name and date range.",
        {"customer": S, "date_from": {"type": "string", "description": "YYYY-MM-DD"},
         "date_to": {"type": "string", "description": "YYYY-MM-DD"}}, []),
    _fn("list_sales_orders", "List sales documents (quotations AND confirmed orders) newest first, optionally for one customer "
        "and/or state (draft, sent, sale, cancel) and date range.",
        {"customer": S, "state": S, "date_from": {"type": "string", "description": "YYYY-MM-DD"},
         "date_to": {"type": "string", "description": "YYYY-MM-DD"}}, []),
    _fn("get_sales_order", "Full detail of one sales order/quotation including its lines.", {"order_id": I}, ["order_id"]),
    _fn("calendar_availability", "Free slots and busy events for the user on a day.",
        {"date": {"type": "string", "description": "YYYY-MM-DD"}, "duration_min": I}, ["date"]),
    _fn("propose_create_product", "Propose creating a new sellable product. Needs a name and a price from the user. "
        "Requires user confirmation. Set allow_similar=true ONLY if the user confirmed it is a different product from a similar existing one.",
        {"name": S, "price": N, "description": S, "default_code": S, "allow_similar": {"type": "boolean"}}, ["name", "price"]),
    _fn("propose_sales_order", "Propose a draft sales order. Each line is EITHER an existing product_id (from search_products) "
        "OR new_product {name, price, description} when the product does not exist and the user gave its price. "
        "Requires user confirmation.",
        {"customer_id": I, "lines": {"type": "array", "items": {"type": "object", "properties": {
            "product_id": I, "quantity": N,
            "new_product": {"type": "object", "properties": {"name": S, "price": N, "description": S,
                                                   "allow_similar": {"type": "boolean"}}, "required": ["name", "price"]}},
            "required": ["quantity"]}}},
        ["customer_id", "lines"]),
    _fn("propose_calendar_event", "Propose booking a calendar event. Requires user confirmation.",
        {"name": S, "start": {"type": "string", "description": "ISO local datetime, e.g. 2026-10-16T14:00"},
         "duration_min": I, "attendee_ids": {"type": "array", "items": I}, "description": S}, ["name", "start"]),
    _fn("propose_log_activity", "Propose logging a call/activity on a CRM lead. Requires user confirmation.",
        {"lead_id": I, "summary": S, "note": S, "date": {"type": "string", "description": "YYYY-MM-DD"}},
        ["lead_id", "summary"]),
    _fn("propose_confirm_order", "Propose confirming an existing quotation into a sales order. Requires user confirmation.",
        {"order_id": I}, ["order_id"]),
]


def _date(v: str | None) -> dt.date | None:
    return dt.date.fromisoformat(v) if v else None


KIND = {"search_customers": "customer", "search_products": "product", "search_leads": "lead"}


NEXT_STEP = {
    "product": "It does not exist. Ask the user whether to create it as a NEW product (a price is needed; if they already "
               "gave one, call propose_sales_order with a new_product line, or propose_create_product).",
    "customer": "That customer does not exist and customers cannot be created here. Tell the user and ask for another name.",
    "lead": "No such lead exists. Tell the user and ask for another name.",
}


def suggestion(kind: str, query: str, rows: list[dict], fuzzy: set, rejected: set | None = None) -> dict[str, Any]:
    """Close-but-not-exact matches. The model must ask the user; ids are blocked from writes until the next turn.
    Matches the user already rejected are never offered again."""
    rows = [r for r in rows if (kind, r["id"]) not in (rejected or ())]
    if not rows:
        return {"data": {"exact_match": False, "query": query, "closest_matches": [],
                         "instruction": f"The user rejected the close matches for '{query}'. Do NOT suggest them again. {NEXT_STEP[kind]}"}}
    for r in rows:
        fuzzy.add((kind, r["id"]))
    return {"data": {"exact_match": False, "query": query,
                     "closest_matches": [{"id": r["id"], "name": r["name"], "score": r["score"]} for r in rows],
                     "instruction": f"No exact {kind} named '{query}'. Do NOT use these yet and do NOT say 'not found'. "
                                    f"Ask the user: 'Did you mean {rows[0]['name']}?' (list up to 3 options) and wait for the answer."}}


def _is_fuzzy(rows) -> bool:
    return bool(rows) and rows[0].get("match") == "fuzzy"


def _blocked(fuzzy: set, kind: str, ids) -> str | None:
    bad = [i for i in ids if (kind, i) in fuzzy]
    return (f"ValidationFailed: {kind} id {bad[0]} was only a close match suggested this turn. Ask the user 'Did you mean ...?' "
            "and wait for their answer before using it.") if bad else None


def run_tool(st, caller: str, name: str, args: dict[str, Any], limit: int = 10, fuzzy: set | None = None,
             rejected: set | None = None) -> dict[str, Any]:
    """Returns {'data': ...} or {'error': ...}. 'action' is set when a write was parked."""
    odoo = st.odoo
    fuzzy = fuzzy if fuzzy is not None else set()
    rejected = rejected or set()
    ignore_products = {i for k, i in rejected if k == "product"}
    try:
        if name in KIND:
            q = args.get("query") or ""
            rows = {"search_customers": crm.search_customers, "search_products": sales.search_products,
                    "search_leads": lambda o, q, n: crm.search_leads(o, q or None, n)}[name](odoo, q, limit)
            return suggestion(KIND[name], q, rows, fuzzy, rejected) if _is_fuzzy(rows) else {"data": rows}
        if name in ("list_quotations", "list_sales_orders"):
            who = args.get("customer")
            if who:  # a misheard customer name must produce a question, not an empty list
                found = crm.search_customers(odoo, who, 3)
                if _is_fuzzy(found):
                    return suggestion("customer", who, found, fuzzy, rejected)
            dates = (_date(args.get("date_from")), _date(args.get("date_to")))
            if name == "list_quotations":
                return {"data": sales.list_quotations(odoo, limit, who, *dates)}
            return {"data": sales.list_orders(odoo, limit, who, args.get("state"), *dates)}
        if name == "get_sales_order":
            return {"data": sales.get_order(odoo, int(args["order_id"]))}
        if name == "calendar_availability":
            me = calendar.organizer_partner_id(odoo, odoo.uid or odoo.authenticate())
            return {"data": calendar.availability(odoo, st.settings, [me], _date(args["date"]),
                                                  int(args.get("duration_min", 30)))}
        action = None
        if name == "propose_create_product":
            action = proposals.product(st, caller, args["name"], args["price"], args.get("description", ""),
                                       args.get("default_code", ""), allow_similar=bool(args.get("allow_similar")),
                                       ignore_ids=ignore_products)
        elif name == "propose_sales_order":
            err = (_blocked(fuzzy, "customer", [int(args["customer_id"])])
                   or _blocked(fuzzy, "product", [int(l["product_id"]) for l in args["lines"] if l.get("product_id")]))
            if err:
                return {"error": err}
            action = proposals.order(st, caller, int(args["customer_id"]), args["lines"], ignore_ids=ignore_products)
        elif name == "propose_calendar_event":
            err = _blocked(fuzzy, "customer", [int(x) for x in args.get("attendee_ids", [])])
            if err:
                return {"error": err}
            action = proposals.event(st, caller, args["name"], args["start"], int(args.get("duration_min", 30)),
                                     [int(x) for x in args.get("attendee_ids", [])], args.get("description", ""))
        elif name == "propose_log_activity":
            err = _blocked(fuzzy, "lead", [int(args["lead_id"])])
            if err:
                return {"error": err}
            action = proposals.activity(st, caller, int(args["lead_id"]), args["summary"], args.get("note", ""), args.get("date"))
        elif name == "propose_confirm_order":
            action = proposals.confirm_order(st, caller, int(args["order_id"]))
        else:
            return {"error": f"Unknown tool {name}"}
        return {"data": {"status": "awaiting_user_confirmation", "preview": action.payload}, "action": action}
    except (NotFound, ValidationFailed, PermissionError, KeyError, ValueError, TypeError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
