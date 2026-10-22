# ERPNext Activity Assistant

A natural-language layer on top of **ERPNext (v15)** for a sales team. A salesperson tells a chat bot, by text or voice note, what they did today. An LLM turns it into a structured **Sales Activity** record, and can create or update Leads, Opportunities, Quotations and Sales Orders. They can also ask questions such as *"What quotations did we send last month?"*, answered live from ERPNext.

There is **no separate business database**: ERPNext stays the single source of truth. The middleware only keeps short-lived conversation state (pending confirmations and recent messages) in memory.

```
 Sales user ──text / voice──▶ telegram_bot.py ──POST /v1/prompt──▶ FastAPI middleware ──REST API──▶ ERPNext v15
                                │ voice → speech-to-text             │ LLM tool-calling (Claude or any    Leads · Opportunities
                                │ Telegram id → caller               │ OpenAI-compatible endpoint)        Quotations · Sales Orders
                                ▼                                    │ validation · confirmation          Sales Activity (custom DocType)
                           plain-text reply  ◀───────────────────────┘ rate limit · audit log
```

Every caller acts in ERPNext as **its own ERPNext user** (own API key and secret). ERPNext roles and permissions therefore decide what each person can read and change; the middleware has no shared super-user.

## Quick start (one command)

On Debian/Ubuntu, `run.sh` installs ERPNext if it is missing, starts it, prepares it for the assistant, loads demo data, runs every test and smoke-tests the API:

```bash
cd erpnext
./run.sh              # everything; asks for your sudo password only if system packages are missing
./run.sh help         # install | start | prepare | seed | test | smoke | serve | bot | stop | status
```

It is safe to re-run (finished steps are skipped) and configurable through environment variables such as `SITE_NAME`, `ADMIN_PASSWORD`, `ERP_PORT`, `BENCH_DIR`, `COMPANY_NAME`, `CURRENCY`, and `SKIP_INSTALL=1` when ERPNext is already installed. Logs of the processes it starts are in `erpnext/.run/`. A first install builds ERPNext from source and takes roughly 15 to 30 minutes. The manual steps are in [section 7](#7-setup-step-by-step).

## Contents
1. [What you can do](#1-what-you-can-do)
2. [How a request flows](#2-how-a-request-flows)
3. [Daily-update rules (what the model extracts)](#3-daily-update-rules-what-the-model-extracts)
4. [Data model in ERPNext](#4-data-model-in-erpnext)
5. [Project layout](#5-project-layout)
6. [Prerequisites](#6-prerequisites)
7. [Setup, step by step](#7-setup-step-by-step)
8. [Choosing the language model](#8-choosing-the-language-model)
9. [Configuration reference](#9-configuration-reference)
10. [Running](#10-running)
11. [Using the Telegram bot](#11-using-the-telegram-bot)
12. [Agent tools](#12-agent-tools)
13. [REST API](#13-rest-api)
14. [Security model](#14-security-model)
15. [Demo data and demo prompts](#15-demo-data-and-demo-prompts)
16. [Tests](#16-tests)
17. [Troubleshooting](#17-troubleshooting)
18. [Known limitations and next steps](#18-known-limitations-and-next-steps)

---

## 1. What you can do

| Say (typed or spoken) | What happens |
|---|---|
| "Met with XYZ Interiors today, sent a quotation for 20 workstations, awaiting their BOQ." | One **Sales Activity**: client, type (*Quotation Sent*), date, details, status (*Awaiting Client*). A preview is shown; **yes** saves it. |
| "Met Desert Rose Furnishings today" (a client that does not exist yet) | A **Lead** is created together with the activity. |
| "Create a quotation for Nizwa Builders for 4 executive desks and 4 ergonomic chairs" | A draft **Quotation**. Prices of existing items come from ERPNext, never from the user. |
| "Add an item Glass Whiteboard, price 300" | Creates a **non-stock item** with its selling price. Only callers in `ITEM_CREATORS` (the manager in the demo). |
| "Quote 5 glass whiteboards at 300 each for XYZ Traders" (the item does not exist) | **Inline creation**: one preview, one **yes**; the item (with price) and the quotation are created together. If the quotation fails, the new item is removed again. Item creators only. |
| "Submit quotation SAL-QTN-2026-00009", then "Make it an order" | Submits the draft; creates a draft **Sales Order** from a submitted quotation. |
| "Confirm sales order SAL-ORD-2026-00004" | Submits the order. Only callers listed in `CONFIRM_CALLERS`. |
| "Open an opportunity for XYZ Interiors, 7000, proposal stage", "Move it to negotiation" | Creates or updates an **Opportunity** (stage, status Open/Replied/Closed, amount). |
| "What quotations did we send last month?", "Show all orders finalized this week", "How many quotations and what total?" | Answered from ERPNext lists and per-status counts/totals. |
| "Which meetings did I have this week?" | Answered from Sales Activity records (`mine=true` limits to the signed-in user). |
| "Nazwa Builders" (misheard by voice) | The bot asks *"Did you mean Nizwa Builders?"* instead of answering "not found". |
| Any of the above as a Telegram **voice note** | The bot replies `Heard: …` first so a mis-hearing is easy to spot, then answers. |

Writes always follow **propose → preview → "yes"**. The assistant cannot delete records, and it will say so rather than offering to.

## 2. How a request flows

1. **Telegram** → `telegram_bot.py` looks up the sender's numeric id in `TELEGRAM_USERS` to find the caller. Unlisted users can only use `/id`. Voice notes (max 3 minutes / 10 MB) are downloaded, renamed `.oga` → `.ogg`, and transcribed; real customer and item names are sent to the speech model as a spelling hint.
2. The bot calls `POST /v1/prompt` with the text, a `session_id` (the Telegram user id) and that caller's API key.
3. `app/agent/orchestrator.py` loads the session (recent turns and the client or opportunity already resolved), builds the system prompt (current time, signed-in user, company currency) and asks the LLM what to do. It runs the requested **tools** for up to 6 steps. Read tools return ERPNext data; write tools only create a **pending action**.
4. The reply asks for confirmation. A bare "yes" or "no" is handled in code, not by the LLM: **yes** runs the pending action through `app/services/executor.py` (the only place that writes to ERPNext); **no** cancels it.
5. Every proposal, execution, cancellation and ERPNext error is appended to `audit.log`.

The same operations are available as plain REST endpoints ([section 13](#13-rest-api)) for other clients, with the same two-step safety.

## 3. Daily-update rules (what the model extracts)

A daily update becomes **one** activity record. If it describes several steps (a meeting *and* a quotation sent), the model logs the most significant one and puts the rest in `details`, unless the user clearly wants separate records.

| Activity type | Typical wording |
|---|---|
| Meeting | met, visited, demo, site visit |
| Call | phoned, spoke, had a call |
| Quotation Sent | sent or shared a quotation or offer |
| BOQ Received | the client sent their bill of quantities |
| Order Finalized | the client confirmed or placed an order |
| Other | anything else |

| Status | Meaning |
|---|---|
| Open | just started, or nothing pending |
| Awaiting Client | waiting for the client (BOQ, approval, reply) |
| In Progress | work ongoing on our side |
| Completed | finished or closed |

Logging "sent a quotation" is an **activity**, not a document. A Quotation or Sales Order is created only when the user asks for one **and** gives items and quantities. Aliases such as `quote`, `boq`, `waiting`, `done` are normalised in `app/services/activities.py`.

Worked example. Input: *"Met with XYZ Interiors today, sent a quotation for 20 workstations, awaiting their BOQ."* The model calls `search_clients("XYZ Interiors")`, then `propose_log_activity` with `activity_type = Quotation Sent`, `status = Awaiting Client`, `date = today`, `details = "Met today, sent quotation for 20 workstations, awaiting BOQ."` and the matched Lead id. After **yes**:

```
Done. Logged Quotation Sent with XYZ Interiors on 2026-10-16 (status: Awaiting Client, id SACT-00022).
```

## 4. Data model in ERPNext

The assistant works with standard ERPNext documents plus one small custom DocType created by `scripts/setup_erpnext.py`.

| ERPNext document | Used for |
|---|---|
| Customer, Lead | The *client*. Lookup is by name: exact first (customers, then leads), then fuzzy. |
| Opportunity | Created for a Lead or Customer; stage and status updated later. |
| Quotation, Sales Order | Drafts created by the assistant; submit and quotation → order conversion use ERPNext's own mapping. |
| Item, Item Price | Item lookup and the selling price (Item Price in the *Standard Selling* list, then the item's standard rate). New items are created as **non-stock, sellable** items; ERPNext creates their *Standard Selling* price from the standard rate. |
| Sales Stage | Valid Opportunity stages. |
| **Sales Activity** (custom) | The structured log of what the team did. |

**Sales Activity** fields:

| Field | Type | Notes |
|---|---|---|
| `client_name` | Data (required) | Display name of the client |
| `activity_type` | Select (required) | Meeting, Call, Quotation Sent, BOQ Received, Order Finalized, Other |
| `activity_date` | Date (required) | Default today |
| `status` | Select (required) | Open, Awaiting Client, In Progress, Completed |
| `reference_doctype` / `reference_name` | Select / Dynamic Link | Link to the Customer, Lead or Opportunity |
| `details` | Small Text | The user's facts in one short sentence |
| `owner` | (ERPNext standard) | The ERPNext user who logged it |

Permissions on Sales Activity show the two visibility options: **Sales User** has read/write/create with *if owner* (sees only their own records); **Sales Manager** and System Manager see everything (the combined view).

Choosing where activity lives is a client decision. This build uses a custom DocType because it gives real fields to filter and report on; the alternative is comments on the Opportunity.

## 5. Project layout

```
erpnext/
  app/
    main.py, config.py     App factory; env-driven Settings
    security.py            X-API-Key auth, per-caller rate limit, JSON-lines audit log
    erp/client.py          ERPNext REST adapter (token auth per caller); the only code that talks to ERPNext
    api/routes.py          REST /v1: reads, proposals, action confirm/cancel, /prompt
    services/
      parties.py           customers, leads, opportunities; combined client search; name lookup
      activities.py        Sales Activity: validation, normalisation, listing, creation
      sales.py             items and prices, quotation/order previews, creation, submit, summaries
      search.py            exact search filters + fuzzy ranking ("Did you mean")
      proposals.py         validate a requested write and park it as a pending action
      executor.py          execute a confirmed action (single write path)
    agent/
      llm.py               Claude (Anthropic SDK) and OpenAI-compatible clients
      tools.py             tool schema shown to the LLM, dispatcher, fuzzy-id guard
      orchestrator.py      conversation loop, yes/no handling, system prompt, honesty guards
    store/                 pending actions (TTL, idempotency, caller-bound); sessions (trimmed, expiring)
  scripts/                 setup_erpnext.py, seed_demo.py, reset_demo.py
  telegram_bot.py          Telegram long-polling client (text + voice)
  tests/                   offline suite (in-memory ERPNext stub + scripted LLM) and tests/live (real ERPNext)
```

## 6. Prerequisites

- **ERPNext v15**, reachable over HTTP, with the **setup wizard completed** (a Company must exist). Developed against `version-15` run by `../run_erpnext.sh` (site `mysite.local`, `http://127.0.0.1:8000`, `Administrator` / `admin`).
- **Python 3.10+** (developed on 3.12).
- An **LLM API key**: Claude (Anthropic) or any OpenAI-compatible provider.
- For voice: a speech-to-text key for an OpenAI-compatible `/audio/transcriptions` endpoint (Groq or OpenAI).
- For Telegram: a bot token from [@BotFather](https://t.me/BotFather).

## 7. Setup, step by step

**1. Install**

```bash
cd erpnext
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

**2. Make sure ERPNext is running and set up.** If `http://127.0.0.1:8000/api/method/ping` does not answer `{"message":"pong"}`, start your bench. If `bench start` stops because of the asset watcher (`yarn: not found`), start the processes without it: `bench serve --port 8000`, `bench schedule`, `bench worker`, plus the two bench redis servers.

A brand-new site has not run the setup wizard, and nothing can be created until it has (no Company, fiscal year or price lists). Complete it in the browser, or from the bench directory:

```bash
bench --site mysite.local execute frappe.desk.page.setup_wizard.setup_wizard.setup_complete --kwargs "{'args': {
  'language':'English','country':'Oman','timezone':'Asia/Muscat','currency':'OMR',
  'company_name':'Al Mashani Trading Demo','company_abbr':'AMT','chart_of_accounts':'Standard',
  'fy_start_date':'2026-01-01','fy_end_date':'2026-12-31','full_name':'Admin User',
  'email':'admin@example.com','password':'admin'}}"
```

**3. Prepare ERPNext for the assistant** (idempotent, safe to re-run):

```bash
venv/bin/python scripts/setup_erpnext.py
```

This uses the Administrator login (`ERP_ADMIN_USERNAME` / `ERP_ADMIN_PASSWORD`, default `Administrator` / `admin`) once to:
- create the **Sales Activity** DocType with its permissions;
- grant the Sales User and Sales Manager roles read access to **Item Price** and **Sales Stage**, so previews can show exact totals and valid stages;
- create three users (`alice`, `bob`, `manager`) with API keys; the manager also gets the Sales User and **Item Manager** roles, and Item Manager gets read/write/create/delete on Item Price (delete only undoes an item the app just created);
- write `ERP_USERS`, `API_KEYS`, `CONFIRM_CALLERS`, `ITEM_CREATORS` and `MIDDLEWARE_KEYS` into `.env` (created from `.env.example`, mode 600). Re-running keeps credentials that still work and only generates new ones when needed.

**4. Load demo data** (optional, idempotent):

```bash
venv/bin/python scripts/seed_demo.py
```

**5. Configure the rest of `.env`**: your LLM key ([section 8](#8-choosing-the-language-model)), and for Telegram the bot token and `TELEGRAM_USERS` ([section 11](#11-using-the-telegram-bot)). `.env.example` documents every variable.

**6. Run it** ([section 10](#10-running)).

## 8. Choosing the language model

Set in `.env`; no code change needed. The orchestrator speaks one dialect (OpenAI-style messages and tool calls); `app/agent/llm.py` translates for Claude.

```bash
# Claude (default)
LLM_PROVIDER=anthropic
LLM_API_KEY=sk-ant-...
LLM_MODEL=claude-sonnet-5-5       # claude-haiku-5-5 = lowest cost, claude-opus-5-5 = most capable
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

Notes:
- The Claude adapter never sends `temperature` (current Claude models reject non-default values) and never forces a tool choice. It replays the model's own response blocks unchanged within a tool loop, and turns a safety refusal into a plain reply.
- OpenAI-compatible calls retry once, then fall through `LLM_FALLBACK_MODELS`. HTTP 400/401/403/404 are not retried.
- If the provider is unavailable the API answers `503` and the bot says it is unavailable; nothing is changed.

## 9. Configuration reference

All settings come from environment variables; a `.env` in this directory is loaded automatically (variables already set in the shell win). **`.env.example` has the full list with comments.**

| Group | Variables |
|---|---|
| ERPNext | `ERP_URL`, `ERP_TIMEOUT`, `ERP_USERS` (`caller:api_key:api_secret`), `ERP_ADMIN_USERNAME`, `ERP_ADMIN_PASSWORD` (setup scripts only) |
| API | `API_KEYS` (`key:caller`), `CONFIRM_CALLERS`, `ITEM_CREATORS`, `RATE_LIMIT_PER_MINUTE` (60), `MAX_PAGE_SIZE` (50), `ACTION_TTL_SECONDS` (900), `SESSION_TTL_SECONDS` (3600), `SESSION_MAX_MESSAGES` (12), `TIMEZONE` (`Asia/Muscat`) |
| LLM | `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`, `LLM_FALLBACK_MODELS`, `LLM_TEMPERATURE`, `LLM_EFFORT` |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USERS` (`telegram_id:caller`), `MIDDLEWARE_URL`, `MIDDLEWARE_KEYS` (`caller:api_key`) |
| Speech-to-text | `STT_API_KEY`, `STT_BASE_URL`, `STT_MODEL`, `STT_LANGUAGE` (default `en`; `ar` for Arabic; empty = auto-detect) |
| MCP fallback | `MCP_SERVERS` (`name=command; name2=command`), `MCP_TIMEOUT` (60), `MCP_ROUTER` (1) |

Keep three mappings consistent: `API_KEYS` (`key:caller`) and `MIDDLEWARE_KEYS` (`caller:key`) hold the same keys in opposite order, and every caller must also appear in `ERP_USERS`.

## 10. Running

```bash
# API (interactive docs at http://localhost:8100/docs)
venv/bin/uvicorn app.main:app_from_env --factory --port 8100

# Telegram client (add --verbose for debug logs)
venv/bin/python telegram_bot.py
```

- Health check: `curl localhost:8100/health` → `{"status":"ok"}`.
- Run **only one** bot process per bot token; two pollers compete for updates.
- State (pending actions, sessions, rate-limit counters) is in memory; restarting the API clears it.

Reset and re-seed the demo data at any time:

```bash
venv/bin/python scripts/reset_demo.py --yes   # removes activities, orders, quotations, opportunities and non-demo leads
venv/bin/python scripts/seed_demo.py
```

## 11. Using the Telegram bot

**Bot commands.** `/help` (also `/start`, `help`, `what can you do?`) shows what the bot can do; it is answered by the bot itself, without the AI. `/mcp <request>` sends the request with `force_mcp=true` on `POST /v1/prompt`, so the built-in sales tools are skipped and only the MCP tools are offered (for example `/mcp list all warehouses`); it says so if MCP is not enabled. `/id` works for everyone. The bot registers these in Telegram's `/` menu at start-up.

**First-time pairing.** The bot ignores everyone except users in `TELEGRAM_USERS`. To find an id, send `/id` to the bot (this works for anyone). Add `id:caller` to `TELEGRAM_USERS` (for example `123456789:alice`), then restart the bot. Each Telegram user is mapped to one caller, and that caller is one ERPNext user.

**Conversation rules**
- Write requests answer with a short preview and ask you to reply `yes` or `no`.
- `yes` / `ok` / `sure` / `confirm` execute; `no` / `cancel` / `stop` cancel. Anything else is treated as a new instruction.
- A newer proposal replaces an unanswered older one. Pending actions expire after `ACTION_TTL_SECONDS`.
- If a name is not found exactly, the bot asks *"Did you mean …?"*; answer yes (or give the right name) and it continues. Answer **no** and that record is never suggested again in the conversation; if the client is genuinely new, the activity can create a Lead.
- Replies are plain text: markdown the model sometimes adds (bold, tables) is flattened before sending.

## 12. Agent tools

Read tools run immediately. Write tools only **propose**; the user must confirm.

| Tool | Kind | Purpose |
|---|---|---|
| `search_clients` | read | Customers and Leads by name (each result has an `id` and `type`) |
| `search_opportunities` | read | Opportunities by client name; empty query = latest; optional status |
| `search_items` | read | Sellable items with ERPNext selling prices |
| `list_activities` | read | Sales Activities, filtered by client, type, status, dates, `mine` |
| `list_quotations`, `list_sales_orders` | read | Documents by customer, status, date range; `finalized=true` = submitted orders only |
| `get_document` | read | Full detail of a Quotation, Sales Order, Opportunity or Lead, including item lines |
| `sales_summary` | read | Count and total per status for Quotations, Sales Orders or Opportunities |
| `propose_log_activity` | write | One activity for an existing client, or `new_client_name` to also create a Lead |
| `propose_create_opportunity`, `propose_update_opportunity` | write | Create; change stage, status (Open/Replied/Closed) or amount |
| `propose_create_item` | write | New non-stock item with the price the user gave (item creators only; duplicate and look-alike guard) |
| `propose_create_quotation`, `propose_create_sales_order` | write | Draft documents from item lines: `item_id` (price from ERPNext) or `new_item {name, price}` to create the item inline (item creators only) |
| `propose_submit_document` | write | Submit a draft Quotation or Sales Order (Sales Order: `CONFIRM_CALLERS` only) |
| `propose_quotation_to_order` | write | Draft Sales Order from a **submitted** Quotation (ERPNext's own mapping) |

Guards in code (not left to the model): names that are only close matches are blocked from writes until the user confirms them; rejected suggestions are never offered again; the model cannot ask for "yes" unless a real action is pending; tool results are capped at 4,000 characters; quantities are bounded (0 to 10,000); dates must be valid and not in the past where that matters.

### MCP fallback (optional)

For requests the tools above cannot handle, the assistant can fall back to external [MCP](https://modelcontextprotocol.io) servers. Configure stdio servers in `.env`:

```bash
MCP_SERVERS=fetch=uvx mcp-server-fetch; time=uvx mcp-server-time --local-timezone Asia/Muscat
MCP_TIMEOUT=60
```

How it behaves (`app/agent/mcp.py`, `app/agent/orchestrator.py`):
- **Built-in tools first.** Each prompt runs with the ERPNext tools only. MCP tools (named `mcp__<server>__<tool>`) are offered in a second pass only when nothing was proposed and the model said it cannot do the request, or ran out of steps.
- **Reads run, writes wait for "yes".** A tool is read-only if its MCP `readOnlyHint` says so (a `destructiveHint` always makes it a write), or, without annotations, if its name starts with `get_`, `list_`, `search_`, `read_`, `find_`, `lookup_` and similar. Every other MCP call becomes a pending action of kind `mcp_call`, shown as a preview and executed by `executor.py` after **yes** (or `POST /v1/actions/{id}/confirm`).
- **Servers start lazily** on the first fallback, are restarted if they exit, are skipped for 60 s if they fail to start, and are stopped when the API shuts down. Tool output is capped at 4,000 characters.
- Third-party MCP servers act with **their own credentials**, not the caller's ERPNext user, so ERPNext permissions do not apply to them. Only add servers every caller may use.

**Bundled `frappe` server (any other DocType).** `MCP_SERVERS=frappe=venv/bin/python -m mcp_server.frappe_server` (start the API from `erpnext/`). Tools: `list_docs`, `get_doc`, `count_docs`, `describe_doctype`, `run_report` (read, run at once) and `create_doc`, `update_doc`, `submit_doc`, `cancel_doc` (wait for **yes**; the reply states the exact call). The app sends the caller's name as `params._meta.caller` and the server uses that caller's own `ERP_USERS` key, so ERPNext roles apply; a call without a caller is refused. There is no delete and no arbitrary-method tool. Security DocTypes (Role, DocPerm, API/OAuth, System Settings, logs...) are blocked, User is read-only (ERPNext decides who may read it), Sales Order submit/cancel needs `CONFIRM_CALLERS`, creating Items needs `ITEM_CREATORS`, and `limit` is capped by `MAX_PAGE_SIZE`. The built-in tools' fuzzy-name guard and price-from-ERP rules do **not** apply here; ERPNext's own validation is the backstop.

## 13. REST API

All `/v1/*` endpoints need `X-API-Key: <key>` and count against that caller's rate limit. Interactive docs: `/docs`.

**Reads**

| Endpoint | Parameters |
|---|---|
| `GET /v1/clients` | `q` (2–100 chars), `limit` |
| `GET /v1/opportunities` | `q?`, `status?`, `limit` |
| `GET /v1/items` | `q`, `limit` |
| `GET /v1/activities` | `client?`, `activity_type?`, `status?`, `date_from?`, `date_to?`, `mine?`, `limit` |
| `GET /v1/quotations`, `GET /v1/sales-orders` | `customer?`, `status?`, `date_from?`, `date_to?`, `limit` (sales orders also `finalized?`) |
| `GET /v1/quotations/{id}`, `GET /v1/sales-orders/{id}` | full document with lines |
| `GET /v1/summary` | `doctype`, `customer?`, `date_from?`, `date_to?` |
| `GET /v1/vocabulary` | names used as a speech-recognition hint |

**Proposals** (return `202` with a preview; nothing is written yet; optional `Idempotency-Key` header)

`POST /v1/items` · `POST /v1/activities` · `POST /v1/quotations` · `POST /v1/sales-orders` · `POST /v1/opportunities` · `PATCH /v1/opportunities/{id}` · `POST /v1/quotations/{id}/submit` · `POST /v1/sales-orders/{id}/submit` · `POST /v1/quotations/{id}/to-order`

**Confirmation and chat**: `POST /v1/actions/{id}/confirm` · `POST /v1/actions/{id}/cancel` · `POST /v1/prompt` with `{"message": "...", "session_id": "..."}`.

Example, a two-step write:

```bash
KEY=<an API key from API_KEYS>
# 1. propose: nothing is written
curl -s -X POST localhost:8100/v1/activities -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"activity_type":"Meeting","details":"Showroom review","party_type":"Customer","party_id":"Nizwa Builders"}'
# -> {"action_id":"…","status":"pending","preview":{"client_name":"Nizwa Builders","activity_type":"Meeting",…}}

# 2. confirm: written to ERPNext as that caller's own user
curl -s -X POST localhost:8100/v1/actions/<action_id>/confirm -H "X-API-Key: $KEY"
```

Status codes: `401` bad key, `403` caller may not confirm Sales Orders, `404` not found or action belongs to another caller, `410` action expired or cancelled, `422` validation failed, `429` rate limited, `502` ERPNext unreachable or refused (the reason is included), `503` the LLM is unavailable.

## 14. Security model

- **One ERPNext user per caller.** The middleware holds no admin credentials at run time. ERPNext enforces roles and user permissions on every read and write.
- **Two-step writes**, executed only by `executor.py`, as the same caller who proposed them. Pending actions expire (`ACTION_TTL_SECONDS`), are bound to the caller, and carry an optional idempotency key.
- **No hallucinated confirmations.** The model never relays "reply yes" unless a real pending action exists; ids that were only suggested as close matches in the same turn cannot be used for writes.
- **Prices from ERPNext for existing items** (Item Price, then standard rate); the user's price is never accepted for them. Only callers in `ITEM_CREATORS` (who also need the ERPNext *Item Manager* role) can create a **new** item with a price. A duplicate name is refused, a look-alike name needs the user's confirmation, and an item created inline is removed again (or disabled) if its document fails.
- **Sales Orders** can be submitted only by callers in `CONFIRM_CALLERS`. Nothing can be deleted.
- **Telegram allow-list** by numeric user id; voice files are never downloaded for unlisted users.
- **API-key auth**, a per-caller rate limit, and a JSON-lines `audit.log` of prompts (length only, not text), proposals, executions, cancellations and ERPNext errors.
- **Secrets** live in `.env` (mode 600, git-ignored). Remove `ERP_ADMIN_*` from it after setup in production.
- **MCP fallback** (off unless `MCP_SERVERS` is set): only used after the built-in tools decline; non-read-only MCP tools need **yes** like any other write. MCP servers bypass ERPNext permissions because they use their own credentials, and their output is untrusted text passed back to the model.
- **Data sharing.** Customer names and activity notes are sent to the chosen LLM provider and, for voice, to the speech provider. Confirm that is acceptable under the client's data policy.

## 15. Demo data and demo prompts

`scripts/seed_demo.py` creates (company currency OMR): 6 customers (Nizwa Builders, Gulf Interiors LLC, XYZ Traders, Muscat Office Solutions, Sohar Contracting, Salalah Hospitality Group), 6 items with prices (Workstation 4-Seat 350, Executive Desk 420, Ergonomic Chair 95, Conference Table 780, Filing Cabinet 140, Glass Whiteboard 60), 3 leads (XYZ Interiors, Al Khuwair Developers, Barka Fit-Out Works), 3 opportunities, 5 quotations (4 submitted, 1 draft), 3 sales orders (2 submitted) and 13 sales activities spread over the last five weeks. Transactions are created **as the sales users themselves**, so ownership and permissions look real. It also seeds the data behind the non-sales demo prompts (`docs/prompts.csv`): 4 suppliers, 4 stock items with stock in Stores and Finished Goods, 3 unpaid sales invoices, 3 projects with 6 tasks, 5 employees, 3 notes and 2 to-dos for `manager`, and one extra submitted Sales Order (`SAL-ORD-2026-00004`) that is safe to cancel in the demo. `reset_demo.py` does not remove these extras.

| Caller | ERPNext user | Roles | Can submit Sales Orders |
|---|---|---|---|
| `alice` | alice@example.com | Sales User | no |
| `bob` | bob@example.com | Sales User | no |
| `manager` | manager@example.com | Sales User, Sales Manager | yes |

Things to try:

```
Met with XYZ Interiors today, sent a quotation for 20 workstations, awaiting their BOQ.     (alice)
Had a call with Nazwa Builders this morning, they sent the BOQ over.                        (misheard name)
Visited Desert Rose Furnishings today, they want a quote soon.                              (new client → Lead)
Create a quotation for Nizwa Builders for 4 executive desks and 4 ergonomic chairs.
What quotations did we send last month?
Show all orders finalized this week.                                                        (manager)
How many quotations do we have and what is their total value?
Which meetings did I have this week?                                                        (own vs manager view)
Move the Gulf Interiors opportunity to the proposal stage.
```

## 16. Tests

```bash
venv/bin/pytest tests --ignore=tests/live     # offline: no network, no ERPNext (stubbed ERPNext, scripted LLM)
venv/bin/pytest tests/live -v                 # against a running, seeded ERPNext
venv/bin/pytest tests                         # everything
```

- **Offline (111 tests):** agent conversations (confirmation, cancellation, fuzzy-name guards, permissions, session isolation), REST API (auth, idempotency, caller binding, error mapping, rate limit, audit log), services (search, previews, summaries, activity rules), both LLM adapters (request shapes, tool-call parsing, fallbacks, refusals), the MCP fallback (stdio client against a fake server, read/write classification, yes-gate, restart, shutdown) and the Telegram bot.
- **Live (13 tests):** real ERPNext calls as each of the three users: authentication, fuzzy search, ERPNext prices, summaries, own-vs-manager activity visibility, the quotation → submit → order → confirm lifecycle, opportunity create/update, and error mapping. They **delete the records they create**, using the administrator login in `.env`. They skip automatically when ERPNext or the credentials are missing.

## 17. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ERPNext denied access: … does not have doctype access … Item Price` (or `Sales Stage`) | Sales roles lack read access. Re-run `scripts/setup_erpnext.py`, which grants it. |
| "This caller may not create new items" | The caller is not in `ITEM_CREATORS`. Set it (the demo uses `manager`) and restart; the ERPNext user also needs the *Item Manager* role. |
| `No ERPNext user is configured for caller 'x'` | `x` is missing from `ERP_USERS`. Re-run `setup_erpnext.py`, or add `x:api_key:api_secret`. |
| `ERPNext denied access: Invalid … token` / `401` | The API key or secret no longer matches (for example the user's keys were regenerated). Re-run `setup_erpnext.py`; it replaces credentials that stopped working. |
| `Cannot log in to ERPNext … as admin` (setup scripts) | Wrong `ERP_ADMIN_USERNAME` / `ERP_ADMIN_PASSWORD`. |
| Nothing can be created; missing Company, Fiscal Year or price list | The ERPNext setup wizard has not been completed ([step 2](#7-setup-step-by-step)). |
| `bench start` exits with `yarn: not found` | The asset watcher needs yarn. Start `bench serve`, `schedule`, `worker` and the redis servers separately, or install yarn. |
| `AI service unavailable: … 429` | The LLM provider rate-limited you (free tiers do this quickly). Use a paid key, a fallback model, or wait. |
| `AI service unavailable: … 400 … Tool call validation failed` | The model sent an argument the provider's schema check rejected (for example `null` for a string). Retry; try a stronger model. |
| Bot answers "not authorised" | The Telegram id is not in `TELEGRAM_USERS`. Send `/id`, add `id:caller`, restart the bot. |
| Voice note fails | `STT_API_KEY` is empty or the note is over 3 minutes / 10 MB. |
| "That request has expired" after typing yes | The action outlived `ACTION_TTL_SECONDS`, or the API restarted (state is in memory). Ask again. |
| Quotation totals look low | ERPNext does not fill item rates on REST insert; this app always sends the rate. Totals in previews and summaries are **before tax**. |

## 18. Known limitations and next steps

- **In-memory state.** Pending actions, sessions and rate-limit counters are lost on restart and are not shared across processes. Move them to SQLite or Redis before running more than one worker.
- **Channel.** Telegram uses long polling. WhatsApp or a web app would be another thin client over `/v1/prompt`.
- **Visibility.** Sales Users read Leads, Opportunities, Quotations and Sales Orders according to ERPNext's standard role permissions. For "own records only", add *if owner* permissions or User Permissions in ERPNext; only Sales Activity is restricted out of the box.
- **Opportunity status.** Only Open, Replied and Closed can be set; *Lost* needs lost reasons and is not exposed. Previews and summaries are before tax.
- **New items** are non-stock only (no warehouse or stock entries) and use the default group "Products" and unit "Nos" unless the user says otherwise; the item code is generated from the name.
- **Quotation to order** requires a submitted quotation and a Customer (a Lead must be converted in ERPNext first).
- **LLM coverage.** Verified live against an OpenAI-compatible endpoint (Groq). The Claude adapter is covered by unit tests against the SDK's message shapes but has not been exercised against the live Anthropic API without a key. The server-side refusal-fallback option is not enabled; refusals are returned as a plain message.
- **Voice quality** depends on the speech model, especially for dialect Arabic mixed with English business terms; the `Heard: …` echo is the safeguard.
