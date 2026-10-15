from __future__ import annotations

import datetime as dt
import json
import re
from zoneinfo import ZoneInfo

from app.agent.tools import TOOLS, run_tool
from app.services import executor
from app.services.errors import NotFound, ValidationFailed
from app.odoo.client import OdooError

YES = re.compile(r"^\s*(yes|y|yeah|yep|confirm|confirmed|ok|okay|sure|do it|go ahead)\W*$", re.I)
NO = re.compile(r"^\s*(no|n|nope|cancel|stop|abort|never ?mind)\W*$", re.I)
ASKS_CONFIRM = re.compile(r"\b(reply|say|respond)\b.{0,40}\byes\b|\bconfirm\b.{0,30}\?", re.I | re.S)
MAX_STEPS = 6
MAX_TOOL_RESULT_CHARS = 4000

SYSTEM = """You are a sales assistant connected to Odoo (CRM, Sales, Calendar) for a sales team.
Current time: {now} ({tz}).
Rules:
- Use the tools for ALL facts. Never invent customers, products, prices, ids, or dates.
- Resolve names to ids with the search tools before proposing anything. If several records match or the
  request is missing something (customer, product, quantity, time), ask ONE short clarifying question.
- Write tools only PROPOSE an action; the user must reply yes to execute it. After proposing, summarise
  what will happen in 1-3 short lines and ask the user to reply yes or no.
- If a product the user names is not found, never substitute another one. If the user gave its price, propose the
  order with that line as new_product {{name, price}} (the product is created together with the order); if no price
  was given, ask for the price. Never guess prices for new products. Existing products are priced by Odoo.
- "Orders of a customer" means list_sales_orders (quotations and confirmed); use get_sales_order for line details.
- You cannot delete or edit existing records; say so plainly instead of offering to.
- Amounts are in {currency}; never use any other currency or symbol.
- Names may be misheard from voice ("Nazwa" for "Nizwa"). If a search result has exact_match false, NEVER say not found:
  ask "Did you mean <name>?" (up to 3 options) and wait. After the user confirms, search again using the confirmed
  name before proposing anything. Only set allow_similar=true if the user says a product is different from a similar one.
- Replies are sent to Telegram as plain text: no markdown tables, no bold, short lines.
- Defaults: meetings last 30 minutes unless stated; do not ask for what has a default.
- Resolve relative dates ("tomorrow", "next Monday") from the current time. Keep answers short.
Known from this conversation: {entities}"""


class Orchestrator:
    def __init__(self, st, llm):
        self.st, self.llm = st, llm
        self._currency = None

    def currency(self) -> str:
        """Company currency code, looked up once, so the model never invents a symbol."""
        if self._currency is None:
            try:
                row = self.st.odoo.search_read("res.company", [], ["currency_id"], limit=1)
                self._currency = row[0]["currency_id"][1] if row else "the company currency"
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
            return {"reply": "Cancelled. Nothing was changed in Odoo.", "pending_action": None}
        try:
            result = executor.execute(self.st, action)
        except (NotFound, ValidationFailed) as exc:
            return {"reply": f"I could not do that: {exc}", "pending_action": None}
        except OdooError as exc:
            self.st.audit.write(caller, "odoo_error", action=action.id, error=str(exc))
            return {"reply": "Odoo rejected the request, so nothing was changed.", "pending_action": None}
        action.status, action.result = "done", result
        self.st.audit.write(caller, "execute", action=action.id, kind=action.kind, result=result)
        return {"reply": f"Done. {self._describe(action.kind, result)}", "pending_action": None, "result": result}

    @staticmethod
    def _describe(kind, result):
        if kind == "create_product":
            return f"Product '{result['name']}' created at {result['list_price']} (id {result['id']})."
        if kind == "create_order":
            extra = f" New product(s) created: {len(result['created_product_ids'])}." if result.get("created_product_ids") else ""
            return f"Draft order {result['name']} created (total {result['amount_total']}).{extra}"
        if kind == "confirm_order":
            return f"Order {result['name']} is now {result['state']}."
        if kind == "create_event":
            return f"Event '{result['name']}' booked for {result['start']} UTC."
        return f"Activity {result.get('activity_id')} logged."

    # -- main entry ---------------------------------------------------------------------------
    def handle(self, caller: str, session_id: str, text: str) -> dict:
        st = self.st
        session = st.sessions.get(caller, session_id)
        st.audit.write(caller, "prompt", session=session_id, chars=len(text))
        st.sessions.add(session, "user", text)
        direct = self._resolve_pending(caller, session, text)
        if direct:
            st.sessions.add(session, "assistant", direct["reply"])
            return direct

        tz = st.settings.timezone
        system = SYSTEM.format(currency=self.currency(), now=dt.datetime.now(ZoneInfo(tz)).strftime("%A %Y-%m-%d %H:%M"), tz=tz,
                               entities=json.dumps(session.entities) if session.entities else "nothing yet")
        messages = [{"role": "system", "content": system}, *session.messages]
        pending, failed, nudged = None, [], False
        fuzzy: set = set()  # records only *suggested* this turn; unusable for writes until the user answers
        for _ in range(MAX_STEPS):
            msg = self.llm.chat(messages, TOOLS)
            if not msg.get("tool_calls") and not (msg.get("content") or "").strip():
                msg = self.llm.chat(messages, TOOLS)  # models occasionally return nothing; retry once
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
                break
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for call in calls:
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                out = run_tool(st, caller, call["function"]["name"], args, limit=min(10, st.settings.max_page_size), fuzzy=fuzzy)
                if out.get("action"):
                    if session.pending_action_id:  # a newer proposal supersedes the old one
                        old = st.actions.get(session.pending_action_id, caller)
                        if old and old.status == "pending":
                            old.status = "cancelled"
                    pending = out["action"]
                    session.pending_action_id = pending.id
                if out.get("error") and call["function"]["name"].startswith("propose_"):
                    failed.append(out["error"])
                self._remember(session, call["function"]["name"], out)
                body = json.dumps(out.get("data", out), default=str)[:MAX_TOOL_RESULT_CHARS]
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": body})
        else:
            reply = "That took too many steps. Please try a simpler request."
        if not pending and ASKS_CONFIRM.search(reply):
            # The model must never ask for a "yes" when nothing is actually waiting for confirmation.
            reply = (f"I could not set that up: {failed[-1].split(': ', 1)[-1]}. Nothing was changed." if failed else
                     "I could not prepare that action. Please rephrase with the customer, product and quantity. Nothing was changed.")
        st.sessions.add(session, "assistant", reply)
        return {"reply": reply, "pending_action": (
            {"id": pending.id, "kind": pending.kind, "preview": pending.payload} if pending else None)}

    @staticmethod
    def _remember(session, tool, out):
        data = out.get("data")
        if tool == "search_customers" and isinstance(data, list) and len(data) == 1:
            session.entities["customer"] = {"id": data[0]["id"], "name": data[0]["name"]}
        elif tool == "search_leads" and isinstance(data, list) and len(data) == 1:
            session.entities["lead"] = {"id": data[0]["id"], "name": data[0]["name"]}
