"""Tool schema exposed to the LLM, mapped 1:1 to domain services. Writes only create pending actions."""
from __future__ import annotations

import datetime as dt
from typing import Any

from app.erp.client import ERPError
from app.services import activities, parties, proposals, sales
from app.services.errors import NotFound, ValidationFailed


def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


S, N = {"type": "string"}, {"type": "number"}
DATE = lambda d: {"type": "string", "description": f"YYYY-MM-DD. {d}".strip()}
NEW_ITEM = {"type": "object", "description": "An item that does not exist yet. Needs the price the user gave. Only if item creation is allowed.",
            "properties": {"name": S, "price": N, "description": S, "uom": S, "item_group": S, "allow_similar": {"type": "boolean"}},
            "required": ["name", "price"]}
ITEMS = {"type": "array", "items": {"type": "object", "properties": {"item_id": S, "new_item": NEW_ITEM, "quantity": N},
                                    "description": "Each line has quantity and EITHER item_id (existing item) OR new_item (item to create).",
                                    "required": ["quantity"]}}
PARTY = {"type": "string", "enum": ["Customer", "Lead"]}

TOOLS = [
    _fn("search_clients", "Find Customers and Leads by name. Each result has id and type (Customer or Lead).", {"query": S}, ["query"]),
    _fn("search_opportunities", "Find Opportunities by client name (empty query = latest). Optional status: Open, Quotation, "
        "Converted, Lost, Replied, Closed.", {"query": S, "status": S}, []),
    _fn("search_items", "Find sellable items by name. Returns ids and selling prices.", {"query": S}, ["query"]),
    _fn("list_activities", "List logged sales activities, newest first. Use for 'what did we do with X', "
        "'meetings this week', 'which quotations were sent'. mine=true limits to the current user.",
        {"client": S, "activity_type": {"type": "string", "enum": activities.ACTIVITY_TYPES},
         "status": {"type": "string", "enum": activities.STATUSES}, "date_from": DATE(""), "date_to": DATE(""),
         "mine": {"type": "boolean"}}, []),
    _fn("list_quotations", "List Quotations newest first. Optional customer name, status (Draft, Open, Replied, "
        "Partially Ordered, Ordered, Lost, Cancelled, Expired) and date range.",
        {"customer": S, "status": S, "date_from": DATE(""), "date_to": DATE("")}, []),
    _fn("list_sales_orders", "List Sales Orders newest first. Optional customer name, status (Draft, To Deliver and Bill, "
        "To Bill, To Deliver, Completed, Cancelled, Closed), date range. finalized=true returns only submitted (confirmed) orders.",
        {"customer": S, "status": S, "date_from": DATE(""), "date_to": DATE(""), "finalized": {"type": "boolean"}}, []),
    _fn("get_document", "Full detail of one Quotation, Sales Order, Opportunity or Lead, including item lines.",
        {"doctype": {"type": "string", "enum": ["Quotation", "Sales Order", "Opportunity", "Lead"]}, "id": S}, ["doctype", "id"]),
    _fn("sales_summary", "Count and total value per status for Quotations, Sales Orders or Opportunities in a date range. "
        "Use for 'how many' and 'how much' questions.",
        {"doctype": {"type": "string", "enum": ["Quotation", "Sales Order", "Opportunity"]}, "customer": S,
         "date_from": DATE(""), "date_to": DATE("")}, ["doctype"]),
    _fn("propose_create_item", "Propose creating a new sellable, non-stock item with its selling price (the user must give the price). "
        "Only for callers allowed to create items. Set allow_similar=true ONLY if the user said it is different from a similar existing item. "
        "Requires user confirmation.",
        {"name": S, "price": N, "description": S, "uom": S, "item_group": S, "code": S, "allow_similar": {"type": "boolean"}},
        ["name", "price"]),
    _fn("propose_log_activity", "Propose logging what the user did with a client: one record with activity_type, date, "
        "details and status. For an existing client pass party_type and party_id from search_clients (Opportunity ids are also "
        "accepted with party_type=Opportunity). For a client that does not exist yet and the user clearly means a new one, pass "
        "new_client_name instead (a Lead is created). Requires user confirmation.",
        {"activity_type": {"type": "string", "enum": activities.ACTIVITY_TYPES}, "details": S,
         "status": {"type": "string", "enum": activities.STATUSES}, "date": DATE("Default today."),
         "party_type": {"type": "string", "enum": ["Customer", "Lead", "Opportunity"]}, "party_id": S, "new_client_name": S},
        ["activity_type", "details"]),
    _fn("propose_create_opportunity", "Propose creating an Opportunity for a Lead or Customer. Requires user confirmation.",
        {"party_type": PARTY, "party_id": S, "amount": N, "sales_stage": S, "expected_closing": DATE(""), "notes": S},
        ["party_type", "party_id"]),
    _fn("propose_update_opportunity", "Propose changing an existing Opportunity's sales_stage (Prospecting, Qualification, "
        "Needs Analysis, Value Proposition, Identifying Decision Makers, Perception Analysis, Proposal/Price Quote, "
        "Negotiation/Review), status (Open, Replied, Closed) or amount. Requires user confirmation.",
        {"opportunity_id": S, "sales_stage": S, "status": S, "amount": N}, ["opportunity_id"]),
    _fn("propose_create_quotation", "Propose a draft Quotation for a Customer or Lead with item lines (item_id from search_items; "
        "prices come from ERPNext). Only when items and quantities are clear. Requires user confirmation.",
        {"party_type": PARTY, "party_id": S, "lines": ITEMS, "valid_till": DATE("")}, ["party_type", "party_id", "lines"]),
    _fn("propose_create_sales_order", "Propose a draft Sales Order for an existing Customer. Requires user confirmation.",
        {"customer_id": S, "lines": ITEMS, "delivery_date": DATE("Default in 14 days.")}, ["customer_id", "lines"]),
    _fn("propose_submit_document", "Propose submitting (finalizing) a DRAFT Quotation or Sales Order. Requires user confirmation.",
        {"doctype": {"type": "string", "enum": ["Quotation", "Sales Order"]}, "id": S}, ["doctype", "id"]),
    _fn("propose_quotation_to_order", "Propose creating a draft Sales Order from a SUBMITTED Quotation. Requires user confirmation.",
        {"quotation_id": S, "delivery_date": DATE("Default in 14 days.")}, ["quotation_id"]),
]


def _date(v: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat(v) if v else None
    except ValueError:
        raise ValidationFailed("dates must be YYYY-MM-DD")


NEXT_STEP = {
    "client": "No such Customer or Lead exists. If the user is describing activity with a new client, call propose_log_activity "
              "with new_client_name (it creates a Lead). Otherwise tell the user and ask for another name.",
    "opportunity": "No such opportunity exists. Tell the user and ask for another name.",
    "item": "That item does not exist. If the user gave its price and item creation is allowed for them, create it with propose_create_item "
            "or as a new_item line of the quotation/order; if no price was given, ask for the price. Never guess a price or substitute another item.",
}
KIND = {"search_clients": "client", "search_opportunities": "opportunity", "search_items": "item"}


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
                     "closest_matches": [{"id": r["id"], "name": r["name"], "score": r["score"], **({"type": r["type"]} if "type" in r else {})}
                                         for r in rows],
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
    erp = st.erp_for(caller)
    fuzzy = fuzzy if fuzzy is not None else set()
    rejected = rejected or set()
    ignore_items = {i for k, i in rejected if k == "item"}  # look-alikes the user already said are different
    try:
        if name in KIND:
            q = args.get("query") or ""
            if name == "search_clients":
                rows = parties.search_clients(erp, q, limit)
            elif name == "search_opportunities":
                rows = parties.search_opportunities(erp, q or None, limit, args.get("status"))
            else:
                rows = sales.search_items(erp, q, limit)
            return suggestion(KIND[name], q, rows, fuzzy, rejected) if _is_fuzzy(rows) else {"data": rows}
        if name in ("list_quotations", "list_sales_orders", "list_activities"):
            who = args.get("client") or args.get("customer")
            if who:  # a misheard client name must produce a question, not an empty list
                found = parties.search_clients(erp, who, 3)
                if _is_fuzzy(found):
                    return suggestion("client", who, found, fuzzy, rejected)
            dates = (_date(args.get("date_from")), _date(args.get("date_to")))
            if name == "list_activities":
                return {"data": activities.list_activities(erp, limit, who, args.get("activity_type"), args.get("status"),
                                                           *dates, mine=bool(args.get("mine")))}
            doctype = "Quotation" if name == "list_quotations" else "Sales Order"
            return {"data": sales.list_documents(erp, doctype, limit, who, args.get("status"), *dates,
                                                 finalized=bool(args.get("finalized")))}
        if name == "get_document":
            return {"data": sales.get_document(erp, args["doctype"], str(args["id"]))}
        if name == "sales_summary":
            who = args.get("customer")
            if who:
                found = parties.search_clients(erp, who, 3)
                if _is_fuzzy(found):
                    return suggestion("client", who, found, fuzzy, rejected)
            return {"data": sales.summary(erp, args["doctype"], _date(args.get("date_from")), _date(args.get("date_to")), who)}

        action = None
        if name == "propose_log_activity":
            pid = args.get("party_id")
            err = _blocked(fuzzy, "client", [pid] if pid else []) or _blocked(fuzzy, "opportunity", [pid] if pid else [])
            if err:
                return {"error": err}
            action = proposals.log_activity(st, caller, args["activity_type"], args["details"], args.get("status"), args.get("date"),
                                            args.get("party_type"), pid, args.get("new_client_name"))
        elif name == "propose_create_opportunity":
            err = _blocked(fuzzy, "client", [args["party_id"]])
            if err:
                return {"error": err}
            action = proposals.create_opportunity(st, caller, args["party_type"], args["party_id"], args.get("amount"),
                                                  args.get("sales_stage"), args.get("expected_closing"), args.get("notes", ""))
        elif name == "propose_update_opportunity":
            err = _blocked(fuzzy, "opportunity", [args["opportunity_id"]])
            if err:
                return {"error": err}
            action = proposals.update_opportunity(st, caller, args["opportunity_id"], args.get("sales_stage"),
                                                  args.get("status"), args.get("amount"))
        elif name == "propose_create_item":
            action = proposals.item(st, caller, args["name"], args["price"], args.get("description", ""), args.get("uom"),
                                    args.get("item_group"), args.get("code"), bool(args.get("allow_similar")),
                                    ignore_ids=ignore_items)
        elif name in ("propose_create_quotation", "propose_create_sales_order"):
            party_id = args.get("party_id") or args.get("customer_id")
            err = (_blocked(fuzzy, "client", [party_id])
                   or _blocked(fuzzy, "item", [str(l["item_id"]) for l in args["lines"] if l.get("item_id")]))
            if err:
                return {"error": err}
            if name == "propose_create_quotation":
                action = proposals.quotation(st, caller, args["party_type"], party_id, args["lines"], args.get("valid_till"),
                                             ignore_ids=ignore_items)
            else:
                action = proposals.order(st, caller, party_id, args["lines"], args.get("delivery_date"), ignore_ids=ignore_items)
        elif name == "propose_submit_document":
            action = proposals.submit_document(st, caller, args["doctype"], str(args["id"]))
        elif name == "propose_quotation_to_order":
            action = proposals.quotation_to_order(st, caller, str(args["quotation_id"]), args.get("delivery_date"))
        else:
            return {"error": f"Unknown tool {name}"}
        return {"data": {"status": "awaiting_user_confirmation", "preview": action.payload}, "action": action}
    except NotFound as exc:
        return {"error": f"NotFound: {exc}"}
    except ValidationFailed as exc:
        return {"error": f"ValidationFailed: {exc}"}
    except PermissionError as exc:
        return {"error": f"PermissionDenied: {exc}"}
    except ERPError as exc:
        return {"error": f"ERPNext: {exc}"}
    except (KeyError, TypeError, ValueError) as exc:
        return {"error": f"ValidationFailed: bad or missing argument ({type(exc).__name__}: {exc})"}
