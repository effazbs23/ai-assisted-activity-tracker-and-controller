# Odoo AI middleware (MVP)

Telegram (text or voice) -> FastAPI middleware -> Odoo 20 (CRM, Sales, Calendar). An LLM (Groq) turns messages into
tool calls; every write is a two-step propose, then confirm ("yes"). Details for contributors: `context.md`.

## What it does
- Create sales orders/quotations, including products that do not exist yet (created with the order); create products.
- List a customer's quotations and orders; read leads, products, customers.
- Book calendar events (conflict checked) and see free slots; log calls on CRM leads.
- Voice notes (Groq Whisper), "Did you mean ...?" for misheard names, per-caller API keys, rate limit, audit log.

## Setup
```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env            # fill it in (Odoo admin creds, GROQ_API_KEY, TELEGRAM_BOT_TOKEN, API_KEYS)
venv/bin/python create_ai_user.py   # creates the Odoo user "ai.assistant" + ir.access rows, writes its login to .env
venv/bin/python seed_demo.py        # optional demo customers, products, leads, quotations, events
```
Odoo needs the `crm`, `sale` and `calendar` apps installed.

## Run
```bash
venv/bin/uvicorn app.main:app --port 8000      # API, docs at http://localhost:8000/docs
venv/bin/python telegram_bot.py                # Telegram client (only one poller per bot token)
venv/bin/python -m pytest -q                   # tests (fake Odoo + scripted LLM, no network)
```
First Telegram setup: run the bot with `--allow-all`, send `/id`, put the number in `TELEGRAM_ALLOWED_USER_IDS`, restart without the flag.

## Use
Send prompts to the bot or `POST /v1/prompt` (header `X-API-Key`). Examples are in `DEMO_PROMPTS.md`.
REST: `GET /v1/{customers,products,leads,quotations,sales-orders,vocabulary,calendar/availability}`, `POST /v1/{sales-orders,products,calendar/events,leads/{id}/activities}`
(each returns an `action_id`), then `POST /v1/actions/{id}/confirm|cancel`. Confirming an order needs a key listed in `CONFIRM_CALLERS`.

## Known limits
Pending actions, sessions and rate limits are in memory (lost on restart). All callers act as the one `ai.assistant` Odoo user.
`plan.md` describes moving this into an Odoo module with per-user permissions.
