# ERPNext Activity Assistant

A natural-language layer on top of **ERPNext**. A sales team member tells a chat bot (text or voice) what they did today; an LLM turns it into a structured **Sales Activity** record and can create or update Leads, Opportunities, Quotations and Sales Orders. They can also ask questions ("What quotations did we send last month?") answered live from ERPNext. There is no separate business database: ERPNext stays the single source of truth.

```
 Sales user ──text / voice──▶ telegram_bot.py ──POST /v1/prompt──▶ FastAPI middleware ──REST API──▶ ERPNext v15
                                │ voice → speech-to-text            │ LLM tool-calling (Claude or any   Leads · Opportunities
                                │ Telegram id → caller              │ OpenAI-compatible endpoint)       Quotations · Sales Orders
                                ▼                                   │ validation · confirmation         Sales Activity (custom)
                           plain-text reply  ◀──────────────────────┘ rate limit · audit log
```

Each caller acts in ERPNext as **its own ERPNext user** (own API key and secret), so ERPNext roles and permissions decide what each person can see and change.

## What you can do

| Say (typed or spoken) | What happens |
|---|---|
| "Met with XYZ Interiors today, sent a quotation for 20 workstations, awaiting their BOQ." | One **Sales Activity**: client, type (Quotation Sent), date, details, status (Awaiting Client). Preview, then **yes** to save. |
| "Met Desert Rose Furnishings today" (a client that does not exist) | A **Lead** is created together with the activity. |
| "Create a quotation for Nizwa Builders for 4 executive desks and 4 ergonomic chairs" | Draft **Quotation**; prices come from ERPNext, never from the user. |
| "Submit quotation SAL-QTN-2026-00009", "Make it an order" | Submit a draft; create a draft **Sales Order** from a submitted quotation. |
| "Confirm sales order SAL-ORD-2026-00004" | Submit the order (only callers in `CONFIRM_CALLERS`). |
| "Open an opportunity for XYZ Interiors, 7000, proposal stage" / "Move it to negotiation" | Create / update an **Opportunity**. |
| "What quotations did we send last month?" / "Show orders finalized this week" / "How many quotations and what total?" | Answered from ERPNext (lists and counts/totals). |
| "Meetings I had this week" | Answered from Sales Activity records. |
| "Nazwa Builders" (misheard) | "Did you mean Nizwa Builders?" instead of "not found". |

Writes always follow *propose → preview → "yes"*. The assistant cannot delete records. A daily update is logged as an **activity**, not turned into a quotation, unless the user asks and gives items and quantities.

## Layout

```
app/
  main.py, config.py       App factory and env-driven Settings
  security.py              X-API-Key auth, per-caller rate limit, JSON-lines audit log
  erp/client.py            ERPNext REST adapter (token auth per caller); the only code that talks to ERPNext
  api/routes.py            REST /v1: reads, proposals, action confirm/cancel, /prompt
  services/                parties (customers, leads, opportunities), activities, sales (items, quotations, orders,
                           summaries), search (exact + fuzzy), proposals (validate + park), executor (single write path)
  agent/                   llm.py (Claude and OpenAI-compatible), tools.py (tool schema + guards), orchestrator.py
  store/                   pending actions (TTL, idempotency, caller-bound) and conversation sessions (in memory)
scripts/                   setup_erpnext.py, seed_demo.py, reset_demo.py
telegram_bot.py            Telegram client (text + voice), maps Telegram ids to callers
tests/                     offline suite (in-memory ERPNext stub, scripted LLM) + tests/live (real ERPNext)
```

## Prerequisites

- **ERPNext v15** reachable over HTTP with the setup wizard completed (a Company exists). Developed against `version-15` run by `../run_erpnext.sh` (site `mysite.local`, `http://127.0.0.1:8000`, `Administrator` / `admin`).
- **Python 3.10+**.
- An **LLM API key**: Claude (Anthropic) or any OpenAI-compatible provider.
- For voice: a speech-to-text key (OpenAI-compatible `/audio/transcriptions`, e.g. Groq or OpenAI).
- For Telegram: a bot token from @BotFather.

## Setup

```bash
cd erpnext
python3 -m venv venv && venv/bin/pip install -r requirements.txt

# 1. Prepare ERPNext (idempotent): Sales Activity DocType, 3 users with API keys, read access to Item Price and
#    Sales Stage for sales roles. Writes ERP_USERS, API_KEYS, CONFIRM_CALLERS into .env (created from .env.example).
venv/bin/python scripts/setup_erpnext.py

# 2. Demo data (idempotent): customers, items and prices, leads, opportunities, quotations, orders, activities
venv/bin/python scripts/seed_demo.py

# 3. Add your LLM key to .env (see below), then run
venv/bin/uvicorn app.main:app_from_env --factory --port 8100     # API; docs at http://localhost:8100/docs
venv/bin/python telegram_bot.py                                  # Telegram client
```

`scripts/reset_demo.py --yes` removes activities, orders, quotations, opportunities and non-demo leads so you can seed again.

### Demo users

| Caller | ERPNext user | Roles | Can submit Sales Orders |
|---|---|---|---|
| `alice` | alice@example.com | Sales User | no |
| `bob` | bob@example.com | Sales User | no |
| `manager` | manager@example.com | Sales User, Sales Manager | yes (`CONFIRM_CALLERS=manager`) |

The **Sales Activity** permissions show the visibility options: sales users see only their own activities ("if owner"); the manager sees everyone's (the combined view).

## Choosing the language model

Set in `.env`; no code change needed.

```bash
# Claude (default)
LLM_PROVIDER=anthropic
LLM_API_KEY=sk-ant-...
LLM_MODEL=claude-sonnet-5-5       # or claude-haiku-5-5 for lowest cost, claude-opus-5-5 for the most capable
LLM_EFFORT=medium                 # low | medium | high: thinking depth and cost

# OpenAI
LLM_PROVIDER=openai
LLM_API_KEY=sk-...
LLM_MODEL=gpt-5.4-mini
LLM_TEMPERATURE=                  # empty: some models accept only their default temperature

# Any OpenAI-compatible server (Groq, Azure OpenAI, OpenRouter, Ollama...)
LLM_PROVIDER=openai
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=openai/gpt-oss-120b
LLM_FALLBACK_MODELS=openai/gpt-oss-20b
```

The orchestrator speaks one dialect (OpenAI-style messages and tool calls); `app/agent/llm.py` translates for Claude. Do not set `temperature` for current Claude models: the adapter never sends it.

## Configuration reference

See `.env.example` for every variable. The important ones: `ERP_URL`, `ERP_USERS` (`caller:api_key:api_secret`), `API_KEYS` (`key:caller`), `CONFIRM_CALLERS`, `LLM_*`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USERS` (`telegram_id:caller`), `MIDDLEWARE_KEYS` (`caller:api_key`), `STT_*`, `TIMEZONE` (default `Asia/Muscat`).

To find a Telegram id: message the bot `/id` (works for anyone), then add `id:caller` to `TELEGRAM_USERS` and restart. Unlisted users get nothing else.

## REST API (`X-API-Key` required; interactive docs at `/docs`)

Reads: `GET /v1/clients?q=`, `/v1/opportunities`, `/v1/items?q=`, `/v1/activities`, `/v1/quotations`, `/v1/sales-orders?finalized=true`, `/v1/quotations/{id}`, `/v1/sales-orders/{id}`, `/v1/summary?doctype=`, `/v1/vocabulary`.

Proposals (return `202` with a preview; nothing is written yet): `POST /v1/activities`, `/v1/quotations`, `/v1/sales-orders`, `/v1/opportunities`, `PATCH /v1/opportunities/{id}`, `POST /v1/quotations/{id}/submit`, `/v1/sales-orders/{id}/submit`, `/v1/quotations/{id}/to-order`.

Confirmation: `POST /v1/actions/{id}/confirm`, `/v1/actions/{id}/cancel`. Chat: `POST /v1/prompt {message, session_id}`. Optional `Idempotency-Key` header on proposals.

## Security model

- One ERPNext user per caller; the middleware has no shared super-user. ERPNext enforces roles and user permissions.
- Writes are two-step and executed only by `app/services/executor.py`, as the same caller who proposed them. Pending actions expire (`ACTION_TTL_SECONDS`) and are bound to the caller.
- The LLM never relays "reply yes" unless a real pending action exists. Ids that were only suggested as a close match in the same turn cannot be used for writes.
- Prices always come from ERPNext (Item Price, then the item's standard rate). Quantities are bounded.
- Sales Orders can be submitted only by callers in `CONFIRM_CALLERS`. Nothing can be deleted.
- API-key auth, per-caller rate limit, JSON-lines `audit.log` of proposals, executions, cancellations and ERPNext errors.

## Tests

```bash
venv/bin/pytest tests --ignore=tests/live     # offline, no network (stubbed ERPNext, scripted LLM)
venv/bin/pytest tests/live -v                 # against a running, seeded ERPNext; removes what it creates
venv/bin/pytest tests                         # everything
```

## Known limitations and next steps

- State (pending actions, sessions, rate-limit counters) is in memory; a restart clears it. Move it to SQLite/Redis before running more than one process.
- Telegram uses long polling. WhatsApp or a web app would be another thin client over `/v1/prompt`.
- Sales Users read Leads, Opportunities, Quotations and Sales Orders according to ERPNext's standard role permissions. For "own records only", add "if owner" permissions or User Permissions in ERPNext; only Sales Activity is restricted out of the box.
- Opportunity `status` can be set to Open, Replied or Closed; "Lost" needs lost reasons and is not exposed. Quotation/Sales Order amounts shown in previews are before tax.
- Verified live with an OpenAI-compatible endpoint (Groq). The Claude adapter is covered by unit tests against the SDK's message shapes, but has not been exercised against the live Anthropic API without a key.
- Claude refusals are returned as a plain message; the server-side refusal-fallback option is not enabled.
