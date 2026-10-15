# Plan: move the middleware into an Odoo module (`ai_gateway`) with live, per-user access control

## Context
Today's state: a working external FastAPI middleware (`app/`) + `telegram_bot.py`. It logs in to Odoo as ONE technical user (`ai.assistant`) and enforces limits in its own code. The sketch asks for a different shape: an **Odoo module** that (a) owns the security (`ir.access`), with per-user CRUD toggles that update `ir.access` in real time, (b) exposes REST endpoints for CRM, Sales, Calendar and Invoice, and (c) acts as the middleware, taking the Telegram bot credentials. This replaces the shared-login weakness (every Telegram user currently acts as `ai.assistant`).

## Verdict
**Possible, yes.** Everything in the sketch maps to existing Odoo 20 mechanisms (verified in `/home/anando/brainstation23/odoo`):
- `ir.access` writes clear the access cache immediately (`odoo/addons/base/models/ir_access.py`: `create/write/unlink` call `_clear_caches()`), so "real time" permission changes work.
- Custom controllers can use `auth='bearer'` (Odoo API keys) and run as the real user, so ACLs apply natively (same mechanism as built-in `POST /json/2/<model>/<method>` in `addons/rpc/controllers/json2.py`).
- Telegram identity -> `res.users` mapping lets every action run `with_user(user)` (no sudo), fixing the shared-admin limitation.

Caveat: Odoo 20 already ships `/json/2` for every model with bearer auth + ACL. Custom REST endpoints only earn their keep as **narrow business endpoints** (draft order priced by Odoo, availability/booking, confirm-gate, audit), not as generic CRUD.

## Recommended design
Module `ai_gateway` (depends: `crm`, `sale`, `calendar`, optionally `account`):
1. **Models**
   - `ai.gateway.config` (singleton, group-restricted): Telegram token, webhook secret, Groq key/model. Secrets in `ir.config_parameter`, readable only by the settings group.
   - `ai.gateway.user`: `telegram_user_id` <-> `res.users`, active flag.
   - `ai.gateway.permission`: (gateway user, model, c/r/u/d toggles) -> synced to `ir.access` rows attached to a per-user technical group (`ai_gateway.group_<user>`). Toggle change = create/write/unlink `ir.access` => effective immediately.
   - `ai.gateway.action` (persisted pending actions, TTL, idempotency key) and `ai.gateway.session` (context) and `ai.gateway.audit` (replace in-memory stores and `audit.log`).
2. **Security file** (`security/ir.access.csv` + group XML): base group `AI Gateway User` (implies Role/User), default read-only `ir.access` rows for crm/sale/calendar, and manager group that can edit permissions.
3. **Controllers** (`/ai/v1/...`, `auth='bearer'`, run as the key's user): customers, products, leads, quotations, sales-orders (propose/confirm), calendar availability/events, invoices (read + draft only), `/prompt`, `/actions/{id}/confirm|cancel`. Port `app/services/*`, `proposals`, `executor`, `agent/*` nearly 1:1, replacing `OdooClient` calls with `env[...]` ORM calls.
4. **Telegram transport**: `POST /ai/telegram/webhook` with secret-token header + `update_id` de-dup; handler returns 200 at once and processes via `ir.cron` `_trigger()` (LLM calls take seconds); replies through `sendMessage`. Voice: Telegram `getFile` -> Groq Whisper (port from `live_telegram_odoo_groq_poc.py`).
5. **Reuse**: `app/services/search.py`, `calendar.py` (slot/conflict logic), `sales.py` (validation, qty bounds), `agent/orchestrator.py` (confirmation regex, failed-proposal guard), tests' `ScriptedLLM` pattern (re-expressed as Odoo `TransactionCase`/`HttpCase`).

## Drawbacks and challenges
| Area | Issue | Mitigation |
|---|---|---|
| Hosting | Odoo Online (SaaS) does not allow custom modules; only Odoo.sh / self-hosted do | Confirm hosting first; if SaaS, keep the external middleware |
| Telegram polling | Long polling inside Odoo workers is unreliable (multi-worker duplicates, `limit_time_real` kills) | Use webhook (needs public HTTPS; tunnel for local dev) or keep a tiny external poller forwarding to the webhook |
| Blocking LLM calls | Groq calls take 2-20 s and occupy an HTTP worker; we already saw provider 503s | Ack immediately, process in cron/queue, retry/back-off, per-user rate limit |
| Permissions semantics | Odoo 20: grouped `ir.access` rows are additive (OR); only group-less rows are restrictions and apply to everyone. Base Role/User already grants e.g. customer CRUD, so per-user *revocation* needs a login-keyed restriction (as done for `res.partner`) or a custom base group | Document the model; generate restrictions for models where base grants are broader than wanted |
| Real-time edits | Cache clear is global to the registry, cheap, but multi-worker propagation relies on registry signalling; many edits cause churn | Batch edits; admin-only UI |
| Coupling | Code runs inside the ERP process: a bug or runaway loop affects the whole ERP; `ir.access` is new in Odoo 20 so the module is version-locked | Strict timeouts, small tool surface, tests, version branches |
| State & limits | No Redis; `ormcache` is per-worker, so rate limits/sessions must live in DB | DB tables + cron cleanup; nginx rate limit at the edge |
| Invoices | Posting is legally significant and sequence-consuming; pulls in the heavy `account` module and its groups | Read + draft only; posting stays manual or admin-only with confirm |
| Data & LLM risk | Customer names/notes flow to Groq (PII); prompt injection via CRM text; non-deterministic output; ambiguous names; voice accuracy | Confirmation gate, tool whitelist, per-user ACLs, redact fields sent to LLM, audit log, data-protection sign-off |
| Money/time correctness | Preview excludes tax; per-user timezones; multi-company/currency | Use Odoo `_compute`d totals in preview, `res.users.tz`, company-scoped domains |
| Testing/ops | Odoo test framework is slower; Telegram needs simulation; secrets rotation | `HttpCase` tests with fake Telegram/LLM; config UI for rotation |

## Phasing (each phase shippable)
1. Module skeleton, groups, `ir.access` data, config + user-mapping models, permission-toggle -> `ir.access` sync (+ tests proving a toggle changes behavior immediately).
2. REST controllers for crm/sale/calendar running as bearer-key user; pending-action + audit models; port services.
3. `/prompt` orchestrator + sessions in DB.
4. Telegram webhook + async processing + voice.
5. Invoice (read/draft), hardening (rate limits, de-dup, redaction).
Keep the current FastAPI service running until phase 4 passes, then retire it.

## Verification
- `odoo-bin -c config/odoo20.conf -d dino -i ai_gateway --test-tags ai_gateway --stop-after-init`: unit/HTTP tests for ACL enforcement per user, toggle propagation, confirmation gate, idempotency, webhook de-dup.
- Live: two Odoo users with different permissions, each mapped to a Telegram ID; same prompt allowed for one and denied for the other; flip a toggle and re-run without restart.
- Re-run `DEMO_PROMPTS.md` scenarios through the webhook (simulated Telegram payloads), then once with the real bot.

## Decisions needed before coding
1. Where does the client's Odoo run (Online vs Odoo.sh/self-hosted)?
2. Move LLM orchestration into Odoo, or keep it external and make the module only the secure API + permission layer (lower risk)?
3. Is the Telegram webhook (public HTTPS) acceptable, or must it stay on a local network?
4. Invoice scope: read-only, draft, or posting?
