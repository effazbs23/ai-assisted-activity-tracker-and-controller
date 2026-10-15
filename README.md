# AI-Assisted Activity Tracker and Controller

A middleware that lets a sales team work in Odoo 20 by **chatting with a Telegram bot** (text or voice notes). An LLM (Groq) turns each message into calls on a small, validated set of Odoo operations: CRM, Sales and Calendar. Nothing is written to Odoo until the user confirms with "yes".

```
 Telegram user ──text / voice──▶ telegram_bot.py ──POST /v1/prompt──▶ FastAPI middleware ──JSON-RPC──▶ Odoo 20
                                   │ voice → Groq Whisper               │ Groq LLM tool-calling          crm · sale · calendar
                                   │ allowlist by Telegram user id      │ validation · confirmation
                                   ▼                                    │ rate limit · audit log
                              plain-text reply  ◀───────────────────────┘
```

> Design notes for contributors are in [`context.md`](context.md); sample prompts in [`DEMO_PROMPTS.md`](DEMO_PROMPTS.md); a possible future redesign (an Odoo module with per-user permissions) in [`plan.md`](plan.md).

## Contents
1. [What you can do](#1-what-you-can-do)
2. [How a request flows](#2-how-a-request-flows)
3. [Project layout](#3-project-layout)
4. [Prerequisites](#4-prerequisites)
5. [Setup](#5-setup)
6. [Configuration reference](#6-configuration-reference)
7. [Running](#7-running)
8. [Using the Telegram bot](#8-using-the-telegram-bot)
9. [REST API reference](#9-rest-api-reference)
10. [Agent tools](#10-agent-tools)
11. [Security model](#11-security-model)
12. [Data accuracy rules and limits](#12-data-accuracy-rules-and-limits)
13. [Tests](#13-tests)
14. [Troubleshooting](#14-troubleshooting)
15. [Known limitations](#15-known-limitations)

---

## 1. What you can do

| Area | Examples (typed or spoken) |
|---|---|
| **Sales orders** | "Create a quotation for Nizwa Builders for 4 executive desks and 4 ergonomic chairs." Creates a **draft** `sale.order`; prices come from Odoo. |
| **Products** | "Add a product Standing Desk Pro, price 1200, electric 140cm." |
| **Sell a product that does not exist yet** | "Sell 2 Glass Whiteboard at 300 each to Nizwa Builders." The product is created together with the order (and archived again if the order fails). |
| **Look up** | "Show all sales orders of Gulf Interiors", "Details of order S00007", "Which quotations did we send in the last 7 days?", "List my open leads." |
| **Calendar** | "Book a meeting with Gulf Interiors tomorrow at 11am called Showroom review", "When am I free tomorrow afternoon?" Overlaps are refused. |
| **CRM activity** | "I had a call with XYZ Traders today at 11am about the BOQ, log it." Creates a `mail.activity` on the matching lead. |
| **Misheard names** | "Show orders of Nazwa Builders" → *"Did you mean Nizwa Builders?"* instead of "not found". |
| **Voice** | Send any of the above as a Telegram voice note; the bot replies "Heard: …" and then answers. |

Writes always follow *propose → preview → "yes"*. The assistant cannot delete or edit existing records.

## 2. How a request flows

1. **Telegram** → `telegram_bot.py` checks the sender's numeric ID against `TELEGRAM_ALLOWED_USER_IDS`. Voice notes are downloaded and transcribed by Groq Whisper (`.oga` is renamed `.ogg`, which Groq accepts; real customer/product names are sent as a spelling hint).
2. The bot calls `POST /v1/prompt` with the text and a `session_id` (the Telegram user id).
3. `app/agent/orchestrator.py` loads the session (recent turns, known customer/lead), asks the LLM what to do, and runs the requested **tools** (`app/agent/tools.py`). Read tools return data; write tools only create a **pending action**.
4. The reply asks for confirmation. A "yes" / "no" message is handled in code (not by the LLM): `yes` executes the pending action through `app/services/executor.py`, the only place that writes to Odoo; `no` cancels it.
5. Every proposal, execution, cancellation and Odoo error is appended to `audit.log`.

The same operations are available as plain REST endpoints (section 9) for other clients, with the same two-step safety.

## 3. Project layout

```
app/
  main.py              App factory: wires settings, Odoo client, stores, limiter, audit, agent; /health
  config.py            Settings.from_env() and a tiny .env loader
  security.py          X-API-Key auth, per-caller rate limit, JSON-lines audit log
  odoo/client.py       JSON-RPC adapter (execute_kw); OdooAPI protocol; the only code that talks to Odoo
  api/routes.py        REST /v1: reads, proposals, action confirm/cancel, /prompt
  services/
    crm.py             customers, leads, activity logging, vocabulary
    sales.py           products, order preview/creation/confirm, order lists and detail
    calendar.py        availability, conflict checks, event creation (timezone aware)
    search.py          exact search domain + fuzzy ranking ("Did you mean")
    proposals.py       validate a requested write and park it as a pending action
    executor.py        execute a confirmed action (single write path)
    errors.py          NotFound, ValidationFailed
  agent/
    llm.py             Groq chat client with retry and model fallback
    tools.py           tool schema shown to the LLM + dispatcher + fuzzy-id guard
    orchestrator.py    conversation loop, yes/no handling, system prompt, honesty guards
  store/
    actions.py         pending actions: TTL, idempotency key, bound to the caller
    sessions.py        per caller+session context, trimmed and expiring
telegram_bot.py        Telegram long-polling client (text + voice), plain-text formatting
create_ai_user.py      Creates the Odoo user, group and ir.access rows; writes the login to .env
seed_demo.py           Idempotent demo customers, products, leads, quotations and events
tests/                 68 tests with a fake Odoo and a scripted LLM (no network)
```

## 4. Prerequisites

- **Odoo 20** reachable over HTTP (default `http://127.0.0.1:8069`) with the **CRM**, **Sales** and **Calendar** apps installed, and a database you can log in to as an administrator. (The access-rule script uses Odoo 20's `ir.access` model.)
- **Python 3.10+** (developed on 3.12).
- A **Groq API key** (<https://console.groq.com/keys>): used for the LLM and for voice transcription.
- A **Telegram bot token** from @BotFather (only for the Telegram client).

## 5. Setup

```bash
git clone https://github.com/effazbs23/ai-assisted-activity-tracker-and-controller.git
cd ai-assisted-activity-tracker-and-controller
git checkout development

python3 -m venv venv
venv/bin/pip install -r requirements.txt

cp .env.example .env     # then fill in the values below
```

Minimum to fill in before the next step: `ODOO_URL`, `ODOO_DB`, `ODOO_ADMIN_USERNAME`, `ODOO_ADMIN_PASSWORD`, `GROQ_API_KEY`, `API_KEYS`, and (for Telegram) `TELEGRAM_BOT_TOKEN`.

- **`API_KEYS`** is a comma-separated list of `key:caller` pairs, for example `s3cr3t-key-1:telegram,s3cr3t-key-2:admin`. Generate keys with `python3 -c "import secrets;print(secrets.token_urlsafe(24))"`.
- Set `MIDDLEWARE_API_KEY` to the key that belongs to the `telegram` caller.
- Put any caller that may **confirm** orders (not just draft them) in `CONFIRM_CALLERS`, for example `admin`.

Create the dedicated Odoo user and its access rules (uses the admin credentials once):

```bash
venv/bin/python create_ai_user.py
```

This creates the group **"AI Assistant (Middleware)"**, the user **`ai.assistant`**, the `ir.access` rows listed in [section 11](#11-security-model), and writes `ODOO_USERNAME` / `ODOO_PASSWORD` into `.env`. It is safe to re-run, but each run **rotates the user's password** and rewrites `.env`; restart the API afterwards.

Optional demo data (customers, products, leads, quotations, tomorrow's calendar events):

```bash
venv/bin/python seed_demo.py
```

## 6. Configuration reference

All settings come from environment variables (a `.env` file in the project root is loaded automatically). Variables already set in the shell win over `.env`.

| Variable | Default | Purpose |
|---|---|---|
| `ODOO_URL` | `http://127.0.0.1:8069` | Odoo base URL (no trailing path) |
| `ODOO_DB` | – | Odoo database name |
| `ODOO_USERNAME` / `ODOO_PASSWORD` | – | Login the **middleware** uses (the `ai.assistant` user; a password or an Odoo API key) |
| `ODOO_ADMIN_USERNAME` / `ODOO_ADMIN_PASSWORD` | – | Only for `create_ai_user.py` and `seed_demo.py` |
| `ODOO_TIMEOUT` | `20` | Seconds before an Odoo call fails |
| `API_KEYS` | – | `key:caller` pairs; a request needs a matching `X-API-Key` |
| `CONFIRM_CALLERS` | – | Callers allowed to confirm a quotation into a sales order |
| `RATE_LIMIT_PER_MINUTE` | `60` | Requests per caller per minute (429 beyond that) |
| `MAX_PAGE_SIZE` | `50` | Upper bound for any `limit` query parameter |
| `ACTION_TTL_SECONDS` | `900` | How long a pending action can be confirmed |
| `SESSION_TTL_SECONDS` | `3600` | Idle time before a conversation context expires |
| `SESSION_MAX_MESSAGES` | `12` | Recent messages kept per conversation |
| `TIMEZONE` | `Asia/Dhaka` | Timezone for dates and times the user speaks or types (Odoo stores UTC) |
| `WORK_START_HOUR` / `WORK_END_HOUR` | `9` / `18` | Window used when listing free calendar slots |
| `GROQ_API_KEY` | – | Groq key (LLM and transcription) |
| `GROQ_CHAT_MODEL` | `openai/gpt-oss-20b` | LLM; falls back to `openai/gpt-oss-120b`, then `qwen/qwen3.8-27b` |
| `GROQ_TRANSCRIBE_MODEL` | `whisper-large-v3` | Voice model (handles Bengali and English better than the turbo variant) |
| `GROQ_TRANSCRIBE_LANGUAGE` | – | Optional ISO-639-1 hint (`en`, `bn`) to force a language |
| `TELEGRAM_BOT_TOKEN` | – | BotFather token |
| `TELEGRAM_ALLOWED_USER_IDS` | – | Comma-separated numeric Telegram user IDs that may use the bot |
| `TELEGRAM_ALLOW_ALL` | `0` | `1` lets anyone in; only for first pairing (or use `--allow-all`) |
| `MIDDLEWARE_URL` / `MIDDLEWARE_API_KEY` | `http://127.0.0.1:8000` / – | Where and how the bot reaches the API |

## 7. Running

```bash
# 1. Odoo must be running (your own setup), then:
venv/bin/uvicorn app.main:app --port 8000       # API; interactive docs at http://localhost:8000/docs
venv/bin/python telegram_bot.py                 # Telegram client (add --verbose for debug logs)
```

- Health check: `curl localhost:8000/health` → `{"status":"ok"}`.
- Run **only one** bot process per bot token; two pollers compete for updates.
- State (pending actions, sessions, rate-limit counters) is in memory, so restarting the API clears it.

## 8. Using the Telegram bot

**First-time pairing.** The bot ignores everyone except allowlisted users. To find your ID:

```bash
venv/bin/python telegram_bot.py --allow-all     # temporary
```

Send `/id` to the bot, copy the number into `TELEGRAM_ALLOWED_USER_IDS`, stop the bot and start it again **without** `--allow-all`. Unlisted users can still send `/id`, nothing else.

**Conversation rules**
- Write requests answer with a short preview and ask you to reply `yes` or `no`.
- `yes` / `ok` / `sure` / `confirm` execute; `no` / `cancel` / `stop` cancel. Anything else is treated as a new instruction.
- A newer proposal replaces an unanswered older one. Pending actions expire after `ACTION_TTL_SECONDS`.
- If a name is not found exactly, the bot asks *"Did you mean …?"*; answer yes (or give the right name) and it continues. Answer **no** and that record is never suggested again in the conversation: for a product the bot offers to create it as a new one (it asks for the price if you haven't given it).
- Voice notes up to 3 minutes / 10 MB are accepted; the bot shows the transcript first so you can spot a mis-hearing.

More example prompts: [`DEMO_PROMPTS.md`](DEMO_PROMPTS.md).

## 9. REST API reference

All `/v1/*` endpoints require the header `X-API-Key: <key>` and count against the caller's rate limit. Interactive docs: `/docs`.

**Reads**

| Method & path | Parameters | Returns |
|---|---|---|
| `GET /v1/customers` | `q` (2–100 chars), `limit` | Matching customers; if none match exactly, up to 3 close names flagged `"match": "fuzzy"` |
| `GET /v1/products` | `q`, `limit` | Sellable products with `list_price` (fuzzy fallback as above) |
| `GET /v1/leads` | `q?`, `limit` | CRM leads (matches the lead name, then the customer name, then fuzzy) |
| `GET /v1/quotations` | `customer?`, `date_from?`, `date_to?`, `limit` | Draft and sent quotations |
| `GET /v1/sales-orders` | `customer?`, `state?` (`draft`, `sent`, `sale`, `cancel`), `date_from?`, `date_to?`, `limit` | All quotations and orders, newest first |
| `GET /v1/sales-orders/{id}` | – | One order with its lines |
| `GET /v1/calendar/availability` | `date`, `duration_min` (5–480) | Busy events and free slots inside working hours |
| `GET /v1/vocabulary` | `limit` | Customer and product names (used as a speech hint) |
| `GET /health` | – | Liveness, no auth |

**Writes: always two steps.** Each of these returns `202` with `{"action_id", "status": "pending", "preview"}` and changes nothing in Odoo:

| Method & path | Body |
|---|---|
| `POST /v1/sales-orders` | `{"customer_id": 6, "lines": [{"product_id": 1, "quantity": 20}, {"new_product": {"name": "Glass Whiteboard", "price": 300}, "quantity": 4}]}`; each line has **exactly one** of `product_id` / `new_product` |
| `POST /v1/products` | `{"name", "price", "description?", "default_code?", "allow_similar?"}` |
| `POST /v1/calendar/events` | `{"name", "start": "2026-10-16T11:00", "duration_min": 30, "attendee_ids": [6], "description?"}` (naive times are read in `TIMEZONE`) |
| `POST /v1/leads/{id}/activities` | `{"summary", "note?", "date?"}` |
| `POST /v1/sales-orders/{id}/confirm` | – (only callers in `CONFIRM_CALLERS`, otherwise `403`) |

Then execute or discard:

| Method & path | Result |
|---|---|
| `POST /v1/actions/{action_id}/confirm` | Performs the write and returns `{"status": "done", "result": …}`. Repeating the call returns the same result without writing again. |
| `POST /v1/actions/{action_id}/cancel` | Discards a pending action |

Send an `Idempotency-Key` header on any proposal to make retries return the same `action_id`.

**Natural language**

| Method & path | Body | Returns |
|---|---|---|
| `POST /v1/prompt` | `{"message": "…" (≤2000 chars), "session_id": "…"}` | `{"reply", "pending_action": {"id","kind","preview"} \| null, "session_id"}` |

**Status codes:** `401` bad/missing key · `403` caller may not confirm orders · `404` record or action not found · `410` action cancelled or expired · `422` validation failed or conflict · `429` rate limited · `502` Odoo rejected or is unreachable · `503` the AI service is unavailable.

**Example**

```bash
KEY=your-telegram-key
curl -s -X POST localhost:8000/v1/prompt -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"session_id":"me","message":"Create a quotation for Nizwa Builders for 4 executive desks"}'
# → {"reply":"…Reply yes or no.","pending_action":{"kind":"create_order",…}}
curl -s -X POST localhost:8000/v1/prompt -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"session_id":"me","message":"yes"}'
# → {"reply":"Done. Draft order S00010 created (total 4140.0)."}
```

## 10. Agent tools

The LLM can only call these (defined in `app/agent/tools.py`); there is no generic Odoo access.

| Tool | Kind | Purpose |
|---|---|---|
| `search_customers`, `search_products`, `search_leads` | read | Resolve names to ids; return "closest matches" when only fuzzy matches exist |
| `list_quotations`, `list_sales_orders`, `get_sales_order` | read | Order history and detail |
| `calendar_availability` | read | Free slots for a day |
| `propose_create_product` | write (pending) | New product |
| `propose_sales_order` | write (pending) | Draft order; lines use an existing `product_id` or a `new_product` |
| `propose_calendar_event` | write (pending) | Book a meeting |
| `propose_log_activity` | write (pending) | Log a call/activity on a lead |
| `propose_confirm_order` | write (pending) | Turn a quotation into a sales order (restricted callers) |

## 11. Security model

- **Authentication:** `X-API-Key` per client, mapped to a caller name. The Telegram bot additionally enforces a numeric-user-ID allowlist before anything reaches the API.
- **Least-privilege Odoo identity:** the middleware logs in as `ai.assistant`, not as an administrator. `create_ai_user.py` grants (via Odoo 20 `ir.access`):

  | Models | Operations |
  |---|---|
  | `res.partner` | read (plus a restriction that blocks create/update/delete for this login) |
  | `res.users`, `mail.activity.type`, `ir.model` | read |
  | `product.product`, `product.template` | create, read, update |
  | `crm.lead` | read, update |
  | `sale.order`, `sale.order.line`, `calendar.event` | create, read, update |
  | `mail.activity` | create, read |

  **Nothing can be deleted.** Odoo's own base permissions still apply on top (a group permission can add rights but not remove ones granted elsewhere).
- **Two-step writes:** proposals are validated, stored with a TTL, bound to the caller that created them, and idempotent; only a confirmed action reaches `executor.py`.
- **Honesty guards in the agent:** the model may not ask you to reply "yes" unless a real pending action exists (it is nudged once, then the reply is replaced with a plain error); a record that was only a *fuzzy suggestion* cannot be used for a write in the same turn; failed proposals are reported, not hidden.
- **Rate limiting and caps:** per-caller rate limit, bounded request fields (message ≤ 2000 chars, ≤ 50 order lines), `limit` capped by `MAX_PAGE_SIZE`.
- **Audit log:** `audit.log` (JSON lines) records `prompt`, `propose`, `execute`, `cancel` and `odoo_error` events with caller, action id and result. It is git-ignored.
- **Secrets** live only in `.env` (git-ignored). Customer and product names, and the user's messages, are sent to Groq for processing.

## 12. Data accuracy rules and limits

| Rule | Value |
|---|---|
| Prices for existing products | Always from Odoo; the LLM and the user cannot override them |
| Price for a *new* product | `0 < price ≤ 1,000,000`; required (the assistant asks if missing, never guesses) |
| Quantity per line | `0 < qty ≤ 10,000`; at most 50 lines per order |
| New product names | 2–120 characters; refused if the name already exists (case-insensitive) or is ≥ 85 % similar to an existing product unless the user says it is different (`allow_similar`) |
| Customers/products must exist | Ids are re-validated against Odoo at proposal time |
| Calendar | 5–480 minutes; no booking in the past; the slot is re-checked at confirmation time, so a slot taken in between is refused |
| Times | Naive times are interpreted in `TIMEZONE`; stored in Odoo as UTC |
| Order preview total | Subtotal **excluding tax** (`subtotal_excl_tax`); Odoo adds tax when the order is created |
| Fuzzy name matching | Only when the exact search is empty; similarity ≥ 0.62, up to 3 suggestions, word-order and `LLC`/`Ltd` insensitive |
| Currency in replies | The company currency is given to the LLM, which must not use any other |

## 13. Tests

```bash
venv/bin/python -m pytest -q        # 68 tests, no network or Odoo needed
```

`tests/fake_odoo.py` is an in-memory Odoo with a small domain matcher; `tests/conftest.py` provides a scripted LLM, so agent behaviour (confirmation gate, nudging, fuzzy guard, context isolation, history trimming) is tested deterministically. Coverage: API validation and auth, idempotency and caller binding, calendar conflicts and slot races, products and orders with new products, fuzzy matching, and the Telegram handler (allowlist, voice flow, `.oga` handling, plain-text formatting).

## 14. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `401 Invalid or missing API key` | Header `X-API-Key` missing, or the key is not in `API_KEYS` |
| `502 Odoo is unavailable or rejected the request` | Odoo is down, wrong `ODOO_*` values, or the `ai.assistant` password was rotated (re-run setup, restart the API) |
| `503 AI service unavailable` | Groq rate limit or model error; the message lists each model's error. Retry shortly |
| "You are not allowed to create/delete …" in the audit log | The `ai.assistant` user lacks an `ir.access` row; re-run `create_ai_user.py` |
| Bot silent | Not in `TELEGRAM_ALLOWED_USER_IDS`, a second bot process is polling, or `MIDDLEWARE_API_KEY` is wrong. Run with `--verbose` |
| Voice says "Something went wrong" | Check Groq key/model; unsupported formats are rejected; notes over 3 minutes are refused with a message |
| Wrong language transcript | Use `whisper-large-v3` and, if needed, set `GROQ_TRANSCRIBE_LANGUAGE` |
| "Product … already exists" or "very similar product exists" | Use the existing product, or tell the assistant it is a different product |
| Order total differs from the preview | The preview is before tax |

## 15. Known limitations

- Pending actions, sessions and rate limits are **in memory**: lost on restart and not shared between several API processes.
- All callers act as the **single** `ai.assistant` Odoo user; per-user Odoo permissions would need the Odoo-module design in [`plan.md`](plan.md).
- The Telegram client uses long polling (one process); a public webhook is not implemented.
- Fuzzy matching is string similarity, so it catches "Nazwa" for "Nizwa" but not names that only sound alike. It scans up to 500 names per fallback search.
- LLM output is not deterministic; safety comes from the confirmation step, validation and Odoo permissions rather than from the model.
- Invoices are not covered.
