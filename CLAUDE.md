# CLAUDE.md — AI-Assisted Activity Tracker and Controller

Context for anyone (human or AI) working in this repo. Read this first; deeper docs are linked at the end.

## What this is

A chat-driven sales assistant: a salesperson types or speaks to a **Telegram bot**, an **LLM with tool-calling** turns the message into a small set of validated ERP operations, and a **FastAPI middleware** executes them only after the user replies **"yes"**.

The repo holds **two independent implementations of the same idea**:

| | Odoo version (repo root) | ERPNext version (`erpnext/`) |
|---|---|---|
| ERP | Odoo 20 (CRM, Sales, Calendar) | ERPNext v15 (Lead, Opportunity, Quotation, Sales Order, custom **Sales Activity** DocType) |
| Transport to ERP | JSON-RPC `execute_kw` (`app/odoo/client.py`) | Frappe REST `/api/resource`, `/api/method` (`app/erp/client.py`) |
| ERP identity | **One** shared technical user `ai.assistant` | **One ERPNext user per caller** (own API key/secret) — ERPNext permissions apply natively |
| LLM | Groq only (`openai/gpt-oss-20b` → fallbacks) | `LLM_PROVIDER=anthropic` (Claude SDK, default) or any OpenAI-compatible endpoint |
| Speech-to-text | Groq Whisper (`whisper-large-v3`) | Any OpenAI-compatible `/audio/transcriptions` (`STT_*`) |
| Main use case | Quotations/orders, products, calendar booking, call logging | **Daily activity logging** ("met X, sent quote, awaiting BOQ" → one Sales Activity), plus leads/opps/quotes/orders |
| API port | 8000 | 8100 (`uvicorn app.main:app_from_env --factory`) |
| Default TZ | Asia/Dhaka | Asia/Muscat (demo company is in Oman, OMR) |
| Setup | `create_ai_user.py`, `seed_demo.py` | `./run.sh` (installs bench + ERPNext from scratch), `scripts/setup_erpnext.py`, `seed_demo.py`, `reset_demo.py` |

The two trees **share no code** — `erpnext/app/` is a fork of the root `app/`, so a fix in one usually needs porting to the other (search, store, security, orchestrator guards are near-identical). Each has its own `app` package, `tests/`, `requirements.txt`, `.env`, `telegram_bot.py`.

## Architecture (same in both)

```
Telegram ──▶ telegram_bot.py ──POST /v1/prompt──▶ FastAPI app ──▶ ERP
  text/voice    allowlist, voice→STT,              orchestrator (LLM tool loop)
                "Heard: …" echo, plain()           tools → services → client
```

Layering, top to bottom — keep it this way:

| Layer | Files | Rule |
|---|---|---|
| Transport | `telegram_bot.py` | Thin client; no ERP logic. Long-polling, **one process per bot token**. |
| HTTP | `app/api/routes.py`, `app/security.py` | `/v1/*` needs `X-API-Key` → caller name; per-caller rate limit; JSON-lines `audit.log`. |
| Agent | `app/agent/orchestrator.py`, `tools.py`, `llm.py` | LLM only sees the tool list in `tools.py` — no generic ERP access. |
| Domain | `app/services/*.py` | `proposals.py` validates + parks writes; **`executor.py` is the only module that writes**. |
| Adapter | `app/odoo/client.py` / `app/erp/client.py` | The only code that talks to the ERP. Protocol class (`OdooAPI` / `ERPAPI`) so tests inject fakes. |
| State | `app/store/actions.py`, `sessions.py` | **In-memory**, thread-locked dicts. Lost on restart; not multi-worker safe. |

App wiring is in `create_app(settings, odoo|erp_for, llm)` — everything hangs off `app.state` (`settings`, `odoo`/`erp_for`, `actions`, `sessions`, `limiter`, `audit`, `agent`). Tests pass fakes through these parameters.

## Invariants the code enforces (do not weaken)

1. **Two-step writes.** Every write tool/endpoint returns a `PendingAction` (202 + preview). Only `POST /v1/actions/{id}/confirm` or a bare "yes" runs `executor.execute`. Actions have a TTL, are bound to the caller that created them, support `Idempotency-Key`, and a newer proposal cancels an older unanswered one.
2. **Yes/No is handled in code, not by the LLM** (`YES` / `NO` regexes in the orchestrator).
3. **No hallucinated confirmations.** If the model's reply asks for "yes" (`ASKS_CONFIRM`) but nothing is pending, it is nudged once to call the real `propose_*` tool; if it still fails, the reply is replaced with a plain error.
4. **Fuzzy-name guard.** Exact search first; only if empty, fuzzy (`difflib`, threshold 0.62, ≤3 hits, noise words like LLC/Ltd ignored, scans ≤500 rows). Fuzzy hits are returned as "Did you mean…?" and their ids are **blocked from writes in the same turn**. If the user rejects a suggestion (`REJECT`), it is never offered again in that session.
5. **Prices come from the ERP** for existing products/items. Only a *new* product/item takes the user's price (0 < price ≤ 1,000,000). New names ≥ 0.85 similar to an existing one need `allow_similar=true`; exact duplicates are refused.
6. **Inline creation is compensated.** A product/item created alongside an order/quotation is archived (Odoo) or deleted/disabled (ERPNext) if the document fails.
7. **Nothing is deleted** by the assistant; it cannot edit arbitrary existing records.
8. **Privileged callers.** Confirming/submitting Sales Orders only for `CONFIRM_CALLERS`; creating items (ERPNext) only for `ITEM_CREATORS` (who also need the ERPNext *Item Manager* role).
9. **Bounds.** qty 0–10,000; ≤ 50 order lines; message ≤ 2000 chars; tool results truncated to 4000 chars; `limit` capped by `MAX_PAGE_SIZE`; ≤ 6 LLM tool steps per turn.
10. **Time.** Naive datetimes are local (`TIMEZONE`); Odoo stores UTC. Calendar slots are re-checked at confirm time (race-safe); no booking in the past.
11. **Replies are plain text** for Telegram (`plain()` strips markdown/tables).

## ERP-specific gotchas

**Odoo 20**
- Access uses the new `ir.access` model. A row **with a group is always a permission (OR-ed)**; only **group-less rows are restrictions**. Customer read-only for `ai.assistant` is a group-less restriction keyed on `user.login`.
- Re-running `create_ai_user.py` **rotates the `ai.assistant` password and rewrites `.env`** — restart the API afterwards.
- `res.partner.is_company` is not set on seeded customers; don't filter on it.
- Order preview total is **pre-tax** (`subtotal_excl_tax`).
- Developer's local Odoo: `/home/anando/brainstation23` (`./run_odoo.sh`, DB `dino`).

**ERPNext v15**
- The setup wizard must be complete (Company, fiscal year, price lists) or nothing can be created.
- Sales roles need read on **Item Price** and **Sales Stage** — `setup_erpnext.py` grants it.
- REST insert does not fill item rates; the app always sends `rate`. Prices: *Standard Selling* Item Price, then `standard_rate`.
- Quotation → Sales Order requires a **submitted** quotation and a Customer (not a Lead).
- Opportunity status settable: Open / Replied / Closed only (Lost needs reasons).
- Three config maps must stay consistent: `API_KEYS` (`key:caller`), `MIDDLEWARE_KEYS` (`caller:key`, same keys reversed), `ERP_USERS` (`caller:api_key:api_secret`). Telegram: `TELEGRAM_USERS` (`telegram_id:caller`).
- Demo callers: `alice`, `bob` (Sales User, see own Sales Activities only), `manager` (Sales Manager + Item Manager; in `CONFIRM_CALLERS` and `ITEM_CREATORS`).
- Claude adapter (`AnthropicLLM`) never sends `temperature`, never forces tool choice, uses `output_config.effort`, replays raw response blocks via the private `_raw` key (stripped for OpenAI-compatible calls), and maps `stop_reason == "refusal"` to a plain reply.

**Telegram / voice (both)**
- Telegram voice notes are `.oga`, which Groq rejects → `upload_args()` renames to `.ogg`.
- Voice limit 3 min / 10 MB. Real customer/product names are sent as a Whisper prompt hint (`/v1/vocabulary`).
- Odoo bot: `TELEGRAM_ALLOWED_USER_IDS` allowlist (`--allow-all` only for pairing). ERPNext bot: `TELEGRAM_USERS` map. `/id` works for everyone.

## MCP fallback (ERPNext version, branch `erpnext-mcp`)

Requests the built-in tools can't handle fall back to external MCP servers. Off unless `MCP_SERVERS` is set.

- **Config:** `MCP_SERVERS="name=command args; other=cmd"` (stdio servers; names can't contain `__`), `MCP_TIMEOUT` (60 s). Parsed by `parse_mcp_servers` in `app/agent/mcp.py`.
- **Wiring:** `create_app(..., mcp=None)` builds an `MCPHub` from settings and stores it in `app.state.mcp`. A FastAPI `lifespan` closes the child processes on shutdown. Servers start lazily on first use, are respawned if they exit, and a server that fails to start is skipped for 60 s.
- **Two phases** (`Orchestrator.handle`): phase 1 runs the LLM loop with the built-in `TOOLS` only. Phase 2 runs only if nothing is pending **and** the loop ran out of steps or the reply matches `UNABLE` ("I can't…", "no tool…"). It re-runs with `TOOLS + hub.tools()`. In phase 1, `mcp__*` names go to the built-in dispatcher and are refused.
- **Read vs write:** a `destructiveHint` always makes a tool a write. Otherwise `readOnlyHint` decides, or, with no annotation, an underscore-terminated read prefix (`get_`, `list_`, `search_`, `find_`, `check_`, …). Reads run immediately (`_run_mcp`). Writes go through `proposals.mcp_call` → `PendingAction(kind="mcp_call", payload={tool_name, server, tool, arguments})` → `executor.execute` after **yes**. `MCPError` at execution is turned into `ValidationFailed`.
- **Hub details:** tool names are sanitised to `[a-zA-Z0-9_-]` and tools whose names go over 64 chars are skipped (provider limits). Descriptions are capped at 1000 chars and output at 4000. All access goes through an `RLock`, because sync endpoints run in a threadpool.
- **Bundled `frappe` server:** `erpnext/mcp_server/frappe_server.py` exposes any DocType (list/get/count/describe/report; create/update/submit/cancel after yes; no delete). `MCPHub.call(..., caller)` puts the caller in `tools/call` `params._meta.caller`; the server maps it to that caller's `ERP_USERS` key, so ERPNext permissions apply. It denies security DocTypes (User is read-only) and honours `CONFIRM_CALLERS` / `ITEM_CREATORS`. Fuzzy-name/price guards do not apply to it. Tests: `tests/test_frappe_mcp_server.py`.
- **Tests:** `erpnext/tests/test_mcp.py` drives a real child process, `tests/fake_mcp_server.py`, which logs every `tools/call` to a file so the tests can prove a write did not run before yes.
- **Open design points:** MCP servers use **their own credentials**, so the caller's ERPNext permissions don't apply. Third-party servers have no per-caller gate on writes (the bundled `frappe` server does). MCP output is untrusted text going back into the LLM (prompt injection). `UNABLE` is a regex heuristic: "I can't delete records" also opens phase 2. The Odoo version has no MCP fallback.

## Running and testing

```bash
# Odoo version (repo root)
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env                       # fill ODOO_*, GROQ_API_KEY, API_KEYS, TELEGRAM_*
venv/bin/python create_ai_user.py          # creates ai.assistant + ir.access, writes creds to .env
venv/bin/python seed_demo.py               # optional demo data
venv/bin/uvicorn app.main:app --port 8000
venv/bin/python telegram_bot.py
venv/bin/python -m pytest -q tests         # NOTE: pass `tests` — bare `pytest` from root also collects erpnext/tests and fails on conftest clash

# ERPNext version
cd erpnext
./run.sh                                   # install/start ERPNext, prepare, seed, test, smoke (idempotent); ./run.sh help
venv/bin/uvicorn app.main:app_from_env --factory --port 8100
venv/bin/pytest tests --ignore=tests/live  # offline
venv/bin/pytest tests/live -v              # needs running, seeded ERPNext; cleans up after itself
```

Verified on 2026-10-19 on branch `erpnext-mcp`: root `tests` → **78 passed**; `erpnext` offline → **111 passed** (incl. 16 MCP); `erpnext/tests/live` → 13 skipped (no ERPNext). The root README still says 68 tests.

Test approach: no network. `tests/fake_odoo.py` and `erpnext/tests/stub_erp.py` are in-memory ERPs with tiny domain/filter matchers; `ScriptedLLM` in each `conftest.py` replays queued assistant messages (`tool_call(name, args)` helper). Agent behaviour (confirmation gate, nudging, fuzzy guard, session isolation, history trimming) is tested deterministically. When adding a filter operator in a service, add it to the fake/stub matcher too.

## Conventions

- Python 3.10+ (developed on 3.12), `from __future__ import annotations`, type hints, small functions, dataclasses for config/state. No ORM, no DB — the ERP is the single source of truth.
- Settings come only from env (`Settings.from_env()`; tiny built-in `.env` loader; shell vars win). Add every new setting to the dataclass, `from_env`, `.env.example` and the README config table.
- Services raise `NotFound` / `ValidationFailed` (`app/services/errors.py`) or `PermissionError`; routes map them to 404 / 422 / 403. ERP failures raise `OdooError` / `ERPError` → 502. LLM failures raise `RuntimeError` → 503.
- Adding a write capability = tool schema in `tools.py` + dispatcher branch + validator in `proposals.py` + branch in `executor.py` + `_describe` text in the orchestrator + REST route (optional) + tests with the fake ERP and `ScriptedLLM`.
- Branches: `development` (default, Odoo), `erpnext-dev` (ERPNext), `erpnext-mcp` (ERPNext + MCP fallback).
- Commit messages use bracketed tags: `[ADD]`, `[IMP]`, `[FIX]`, `[REF]`, `[REM]`… with a scope, e.g. `[IMP] erpnext: default voice language to English`.
- Secrets live only in `.env` (git-ignored, mode 600 for ERPNext). `audit.log`, `venv/`, `.run/` are git-ignored.

## Known limitations (both)

In-memory state (single process); Telegram long polling only; customer names and messages go to the LLM/STT provider (PII); fuzzy matching is spelling-based, not phonetic; previews are pre-tax; Odoo version runs everyone as one ERP user (see `plan.md` for the proposed `ai_gateway` Odoo-module redesign with per-user `ir.access`); invoices not covered.

## Where to read more

| File | Contents |
|---|---|
| `README.md` | Odoo version: setup, config reference, REST API, tools, security model, troubleshooting |
| `context.md` | Short contributor notes for the Odoo version (predates this file) |
| `plan.md` | Proposed redesign as an Odoo module (`ai_gateway`) with per-user permissions; open decisions |
| `DEMO_PROMPTS.md`, `erpnext/DEMO_PROMPTS.md` | Prompts to try against the seeded demo data |
| `NON_TECHNICAL_SUMMARY.md` | Business-level overview with diagrams |
| `erpnext/README.md` | ERPNext version: full setup, LLM choice, Sales Activity data model, API, security |
| `erpnext/.env.example` | Every ERPNext setting, commented |
