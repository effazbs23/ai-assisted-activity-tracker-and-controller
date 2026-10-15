# Project Context

MVP: Telegram (text/voice) -> FastAPI middleware -> Odoo 20 (CRM, Sales, Calendar), driven by an LLM (Groq tool-calling).
Built for a sales team that logs work from Telegram. Setup/run/usage: `README.md`; sample prompts: `DEMO_PROMPTS.md`; future direction: `plan.md`.
Odoo lives at `/home/anando/brainstation23` (`./run_odoo.sh`, DB `dino`). Removed POC scripts and old notes are in `../new-project-archive/`.

## Layout
| Path | Role |
|---|---|
| `app/main.py`, `app/config.py` | App factory, env-driven `Settings` |
| `app/api/routes.py` | REST `/v1` (reads, proposals, `/actions/{id}/confirm`, `/prompt`) |
| `app/odoo/client.py` | JSON-RPC adapter (`OdooAPI` protocol); only code that talks to Odoo |
| `app/services/` | `crm`, `sales`, `calendar`, `search` (exact + fuzzy), `proposals` (validate + park write), `executor` (the single place that writes) |
| `app/agent/` | `llm.py` (Groq with fallback), `tools.py` (tool schema, fuzzy guard), `orchestrator.py` (loop, yes/no handling, prompt) |
| `app/store/` | in-memory pending actions (TTL, idempotency, caller-bound) and sessions |
| `app/security.py` | API-key auth, rate limit, `audit.log` |
| `telegram_bot.py` | Thin Telegram client: voice -> Groq Whisper -> `/v1/prompt`, allowlisted by Telegram user id |
| `create_ai_user.py`, `seed_demo.py` | Odoo user/group/`ir.access` setup; demo data |
| `tests/` | Fake Odoo + scripted LLM (no network) |

## Rules the code enforces
- Writes are two-step: propose -> confirm. Nothing reaches Odoo before "yes". Prices come from Odoo; only a *new* product takes the user's price.
- The LLM never relays "reply yes" unless a real pending action exists (nudged once, then replaced with a plain error).
- Close-but-inexact names become "Did you mean X?"; ids only suggested in the same turn cannot be used for writes. New products >= 0.85 similar to an existing one need `allow_similar`.
- Order confirmation only for `CONFIRM_CALLERS`. Caps: quantity, price, page size, rate limit.

## Odoo 20 notes
- The middleware logs in as `ai.assistant` (group "AI Assistant (Middleware)"). `ir.access`: no delete anywhere; customers read-only via a global restriction keyed on the login. A grouped `ir.access` row is always a permission (OR-ed); only group-less rows are restrictions. Re-running `create_ai_user.py` rotates that user's password and rewrites `.env`.
- Telegram voice notes are `.oga`, which Groq rejects; `telegram_bot.upload_args` sends `.ogg`. Use `whisper-large-v3` for Bengali.
- `res.partner.is_company` is not set on seeded customers; do not filter on it.

## Limits
In-memory state (lost on restart); one shared Odoo user; order preview is pre-tax; Groq can rate-limit (API returns 503).
