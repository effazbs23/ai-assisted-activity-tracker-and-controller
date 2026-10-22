from __future__ import annotations

import datetime as dt
import json
import re
from zoneinfo import ZoneInfo

from app.agent.mcp import MCPError
from app.agent.tools import TOOLS, run_tool
from app.erp.client import ERPError
from app.services import executor, proposals, sales
from app.services.errors import NotFound, ValidationFailed

YES = re.compile(r"^\s*(yes|y|yeah|yep|confirm|confirmed|ok|okay|sure|do it|go ahead)\W*$", re.I)
NO = re.compile(r"^\s*(no|n|nope|cancel|stop|abort|never ?mind)\W*$", re.I)
ASKS_CONFIRM = re.compile(r"\b(reply|say|respond)\b.{0,40}\byes\b|\bconfirm\b.{0,30}\?", re.I | re.S)
REJECT = re.compile(r"^\s*(no|nope|nah|not (that|it|this|them)|neither|none|different|a different|another|new)\b", re.I)
# A final reply that admits the built-in tools cannot do the job unlocks the MCP fallback.
UNABLE = re.compile(r"\b(can(?:not|['’]t)|cannot|unable to|not able to|i don['’]?t have|i do not have|"
                    r"no (?:tool|way)|doesn['’]?t (?:support|allow)|does not (?:support|allow)|"
                    r"outside (?:my|the|these)|not something i|isn['’]?t something)\b", re.I)
MAX_STEPS = 6
MAX_TOOL_RESULT_CHARS = 4000

SYSTEM = """You are a sales activity assistant connected to ERPNext (Leads, Opportunities, Quotations, Sales Orders) for a sales team.
Current time: {now} ({tz}). Signed-in user: {user}.
Your main job: the user tells you, in plain words, what they did today; you turn it into structured ERPNext records.
- A daily update like "Met XYZ Interiors today, sent a quotation for 20 workstations, awaiting their BOQ" is ONE activity record:
  client = XYZ Interiors, activity_type = the most important thing that happened, date, details = the user's facts in one
  short sentence, status. If the update has several distinct steps (a meeting AND a quotation sent), log the most significant one
  and put the rest in details, unless the user clearly wants separate records.
- activity_type: Meeting (met, visit, demo), Call (phoned, spoke), Quotation Sent (sent/shared a quotation or offer), BOQ Received
  (client sent their bill of quantities), Order Finalized (client confirmed/placed an order), Other.
- status: Open (just started or nothing pending), Awaiting Client (waiting for the client: BOQ, approval, reply), In Progress
  (work ongoing on our side), Completed (finished or closed).
- Do not create a Quotation or Sales Order from a daily update unless the user asks for it AND gave items and quantities.
  Logging "sent a quotation" is an activity, not a document.
Rules:
- Use the tools for ALL facts. Never invent clients, items, prices, ids, or dates.
- Resolve the client with search_clients before proposing. If the client does not exist and the user is clearly talking about a
  new client, pass new_client_name to propose_log_activity (a Lead is created). If several records match or something essential is
  missing, ask ONE short clarifying question.
- Write tools only PROPOSE an action; the user must reply yes to execute it. After proposing, summarise what will be recorded in
  1-3 short lines and ask the user to reply yes or no.
- You can create and update Opportunities, create draft Quotations and Sales Orders, and submit them; you cannot delete records
  or edit other fields; say so plainly instead of offering to.
- Existing items are priced by ERPNext; never accept a different price for them. Creating new items is allowed for this user: {can_create}.
  If a requested item does not exist and creation is allowed, never substitute another item: if the user gave its price, either call
  propose_create_item, or (when the item is part of a quotation or order) put new_item {{name, price}} on that line so the item and the
  document are created together after ONE yes. If no price was given, ask for it. Never guess a price. In your summary always say which items are NEW and will be created (name and price). If creation is not allowed, say
  an item manager must create it.
- Amounts are in {currency}; never use any other currency or symbol.
- Names may be misheard from voice ("Nazwa" for "Nizwa"). If a search result has exact_match false, NEVER say not found: ask
  "Did you mean <name>?" (up to 3 options) and wait. After the user confirms, search again using the confirmed name before
  proposing anything. If the user says NO to a suggestion, never suggest it again.
- Questions ("What quotations did we send last month?", "Show orders finalized this week") are answered from the list_* and
  sales_summary tools. Turn relative periods into exact dates from the current time. Report counts and totals from the tools only.
- If a request is simply outside what your tools can do (a doctype or operation you have no tool for), say so briefly and honestly
  instead of inventing an answer; extended tools may then become available for it.
- Always reply in English unless the user clearly writes in another language.
- Replies are sent to a chat app as plain text: no markdown tables, no bold, short lines.
Known from this conversation: {entities}"""

MCP_NOTE = ("Extra capability: requests outside the built-in tools can fall back to MCP servers ({servers}); their tools "
            "then appear with names starting mcp__. If you cannot fulfil a request with the tools you have, say so briefly "
            "and honestly instead of inventing an answer. The 'frappe' server reaches any other ERPNext doctype (stock, HR, "
            "accounting, projects, reports) as the current user: call mcp__frappe__describe_doctype before create_doc.")

MCP_FALLBACK = ("[system] The built-in tools cannot handle this request. Extended MCP tools are now available (names start "
                "with mcp__). Use them to help the user. Read-only MCP tools run immediately; any other MCP tool only "
                "PROPOSES the call and the user must reply yes before it runs, so for a create/update/submit/cancel request call the "
                "tool right away; never ask the user in text whether to proceed. Do not apologise; call the right tool now, "
                "or ask one short clarifying question only if a required detail is missing.")


def _bullets(value, prefix: str = "") -> list[str]:
    """Nested dict/list -> flat '• key: value' lines, so chat replies never show JSON."""
    if isinstance(value, dict):
        return [b for k, v in value.items() for b in _bullets(v, f"{prefix}{k}: " if not isinstance(v, (dict, list)) else f"{prefix}{k} ")]
    if isinstance(value, list):
        return [b for i, v in enumerate(value, 1) for b in _bullets(v, f"{prefix}{i}. ")]
    return [] if value in (None, "") else [f"• {prefix}{value}"]


class Orchestrator:
    def __init__(self, st, llm):
        self.st, self.llm = st, llm
        self._currency = None

    def currency(self, caller: str) -> str:
        """Company currency code, looked up once, so the model never invents a symbol."""
        if self._currency is None:
            try:
                self._currency = sales.company_currency(self.st.erp_for(caller)) or "the company currency"
            except Exception:
                return "the company currency"  # do not cache a failure
        return self._currency

    # -- confirmation handling without the LLM ------------------------------------------------
    def _resolve_pending(self, caller, session, text):
        if not session.pending_action_id:
            return None
        yes, no = YES.match(text), NO.match(text)
        if not (yes or no):
            return None
        action = self.st.actions.get(session.pending_action_id, caller)
        session.pending_action_id = None
        if not action or action.status != "pending":
            return {"reply": "That request has expired. Please ask again.", "pending_action": None}
        if no:
            action.status = "cancelled"
            self.st.audit.write(caller, "cancel", action=action.id)
            return {"reply": "Cancelled. Nothing was changed in ERPNext.", "pending_action": None}
        try:
            result = executor.execute(self.st, action)
        except (NotFound, ValidationFailed) as exc:
            return {"reply": f"I could not do that: {exc}", "pending_action": None}
        except ERPError as exc:
            self.st.audit.write(caller, "erp_error", action=action.id, error=str(exc))
            return {"reply": f"ERPNext rejected the request, so nothing was changed. ({exc})", "pending_action": None}
        action.status, action.result = "done", result
        self.st.audit.write(caller, "execute", action=action.id, kind=action.kind, result=result)
        return {"reply": f"Done. {self._describe(action.kind, result)}", "pending_action": None, "result": result}

    @staticmethod
    def _describe(kind, r):
        if kind == "log_activity":
            lead = f" New lead {r['lead_created']} created." if r.get("lead_created") else ""
            return f"Logged {r['activity_type']} with {r['client_name']} on {r['date']} (status: {r['status']}, id {r['id']}).{lead}"
        if kind == "create_item":
            return f"Item '{r['name']}' created (code {r['id']}) with a selling price of {r['list_price']}."
        if kind == "create_document":
            extra = f" New item(s) created: {', '.join(r['created_items'])}." if r.get("created_items") else ""
            return f"Draft {r['doctype']} {r['id']} created for {r['party_name']} (total {r['grand_total']} {r.get('currency') or ''}).{extra}"
        if kind == "submit_document":
            return f"{r['doctype']} {r['id']} is now {r['status']}."
        if kind == "quotation_to_order":
            return f"Draft Sales Order {r['id']} created from the quotation (total {r['grand_total']})."
        if kind == "create_opportunity":
            return f"Opportunity {r['id']} created for {r['party_name']} (stage {r.get('sales_stage')})."
        if kind == "update_opportunity":
            return f"Opportunity {r['id']} updated (stage {r.get('sales_stage')}, status {r.get('status')})."
        if kind == "mcp_call":
            text = str(r.get("content", ""))
            try:  # results are JSON: say what was saved, or list the fields, never dump JSON into the chat
                doc = json.loads(text)
                if isinstance(doc, dict) and doc.get("doctype") and doc.get("name"):
                    text = f"{doc['doctype']} {doc['name']} saved."
                elif isinstance(doc, (dict, list)):
                    text = "\n" + "\n".join(_bullets(doc)[:15])
            except ValueError:
                pass
            return f"MCP tool {r['server']}.{r['tool']} ran. {text[:300]}"
        return "Done."

    # -- main entry ---------------------------------------------------------------------------
    def handle(self, caller: str, session_id: str, text: str) -> dict:
        st = self.st
        session = st.sessions.get(caller, session_id)
        st.audit.write(caller, "prompt", session=session_id, chars=len(text))
        st.sessions.add(session, "user", text)
        if session.suggested and REJECT.match(text):
            session.rejected |= session.suggested  # "no" to a Did-you-mean: never offer those records again
        session.suggested = set()
        direct = self._resolve_pending(caller, session, text)
        if direct:
            st.sessions.add(session, "assistant", direct["reply"])
            return direct

        tz = st.settings.timezone
        system = SYSTEM.format(currency=self.currency(caller), user=caller,
                               can_create="yes" if caller in st.settings.item_creators else "no",
                               now=dt.datetime.now(ZoneInfo(tz)).strftime("%A %Y-%m-%d %H:%M"), tz=tz,
                               entities=json.dumps(session.entities) if session.entities else "nothing yet")
        messages = [{"role": "system", "content": system}, *session.messages]
        mcp = getattr(st, "mcp", None)
        extended = mcp.tools() if mcp else []
        if extended:
            messages.append({"role": "system", "content": MCP_NOTE.format(servers=", ".join(mcp.names))})
        fuzzy: set = set()  # records only *suggested* this turn; unusable for writes until the user answers
        # Phase 1: the built-in tools are the main controller.
        reply, pending, failed, fuzzy, exhausted = self._run(caller, session, messages, TOOLS, fuzzy)
        # Phase 2: the request the main controller cannot handle falls back to the MCP servers.
        if extended and not pending and (exhausted or UNABLE.search(reply or "")):
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": MCP_FALLBACK})
            reply, pending, failed, fuzzy, _ = self._run(caller, session, messages, TOOLS + extended, fuzzy, mcp=mcp)
        if not pending and ASKS_CONFIRM.search(reply):
            # The model must never ask for a "yes" when nothing is actually waiting for confirmation.
            reply = (f"I could not set that up: {failed[-1].split(': ', 1)[-1]}. Nothing was changed." if failed else
                     "I could not prepare that action. Please rephrase with the client and what happened. Nothing was changed.")
        if pending and (notice := self._new_item_notice(pending)) and notice not in reply:
            reply = f"{reply}\n{notice}"  # stated by code, so the user always sees what will be created
        if pending and pending.kind == "mcp_call":  # stated by code: the exact call "yes" will run
            p = pending.payload
            args = dict(p["arguments"])
            lines = _bullets({k: v for k, v in args.items() if k != "data"}) + _bullets(args.get("data"))
            reply = "\n".join([f"I will run {p['server']}.{p['tool']}:", *lines[:20],
                               "Please reply yes to confirm or no to cancel."])
        session.suggested = set(fuzzy)  # what the user is being asked about right now
        st.sessions.add(session, "assistant", reply)
        return {"reply": reply, "pending_action": (
            {"id": pending.id, "kind": pending.kind, "preview": pending.payload} if pending else None)}

    # -- one tool-looping phase ---------------------------------------------------------------
    def _run(self, caller, session, messages, tools, fuzzy: set, mcp=None) -> tuple:
        """Loop the LLM over `tools` until it answers without tool calls. Returns
        (reply, pending_action, failed_proposals, fuzzy_set, steps_exhausted)."""
        st = self.st
        pending, failed, nudged, exhausted = None, [], False, False
        for _ in range(MAX_STEPS):
            msg = self.llm.chat(messages, tools)
            if not msg.get("tool_calls") and not (msg.get("content") or "").strip():
                msg = self.llm.chat(messages, tools)  # models occasionally return nothing; retry once
            calls = msg.get("tool_calls") or []
            if not calls:
                reply = (msg.get("content") or "").strip() or "Sorry, I did not understand that."
                if not pending and not failed and not nudged and ASKS_CONFIRM.search(reply):
                    # Asked for "yes" but never called a propose_* tool: nothing is pending. Make it do the real call.
                    nudged = True
                    messages.append({"role": "assistant", "content": reply})
                    messages.append({"role": "user", "content": (
                        "[system] You asked the user to confirm, but no propose_* tool was called, so nothing is waiting for "
                        "confirmation. Call the correct propose_* tool now (search first if you need ids), or ask a "
                        "clarifying question without asking for yes/no.")})
                    continue
                return reply, pending, failed, fuzzy, False
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls,
                             **({"_raw": msg["_raw"]} if msg.get("_raw") else {})})
            for call in calls:
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                name = call["function"]["name"]
                if name.startswith("mcp__") and mcp is not None:
                    out = self._run_mcp(caller, mcp, name, args)
                else:
                    out = run_tool(st, caller, name, args, limit=min(10, st.settings.max_page_size), fuzzy=fuzzy,
                                   rejected=session.rejected)
                if out.get("action"):
                    if session.pending_action_id:  # a newer proposal supersedes the old one
                        old = st.actions.get(session.pending_action_id, caller)
                        if old and old.status == "pending":
                            old.status = "cancelled"
                    pending = out["action"]
                    session.pending_action_id = pending.id
                if out.get("error") and (name.startswith("propose_") or name.startswith("mcp__")):
                    failed.append(out["error"])
                self._remember(session, name, out)
                body = json.dumps(out.get("data", out), default=str)[:MAX_TOOL_RESULT_CHARS]
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": body})
        else:
            exhausted = True
            reply = "That took too many steps. Please try a simpler request."
        return reply, pending, failed, fuzzy, exhausted

    def _run_mcp(self, caller, mcp, name: str, args: dict) -> dict:
        """An MCP tool call: read-only ones run now; anything else is parked as a pending action."""
        try:
            if mcp.is_read_only(name):
                return {"data": mcp.call(name, args, caller)}
            action = proposals.mcp_call(self.st, caller, name, args)
            return {"data": {"status": "awaiting_user_confirmation", "preview": action.payload}, "action": action}
        except MCPError as exc:
            return {"error": f"MCP: {exc}"}
        except (NotFound, ValidationFailed, PermissionError) as exc:
            return {"error": f"ValidationFailed: {exc}"}

    @staticmethod
    def _new_item_notice(action) -> str:
        p = action.payload
        if action.kind == "create_item":
            new = [p]
        elif action.kind == "create_document":
            new = [l["new_item"] for l in p.get("lines", []) if l.get("new_item")]
        else:
            return ""
        if not new:
            return ""
        what = "; ".join(f"{i['name']} (code {i['code']}) at {i['price']:g}" for i in new)
        return f"New item(s) that will be created: {what}."

    @staticmethod
    def _remember(session, tool, out):
        data = out.get("data")
        if tool == "search_clients" and isinstance(data, list) and len(data) == 1:
            session.entities["client"] = {"id": data[0]["id"], "type": data[0].get("type"), "name": data[0]["name"]}
        elif tool == "search_opportunities" and isinstance(data, list) and len(data) == 1:
            session.entities["opportunity"] = {"id": data[0]["id"], "name": data[0]["name"]}
