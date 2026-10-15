# Demo data and prompts

Seed (idempotent): `venv/bin/python seed_demo.py` — needs Odoo running (`./run_odoo.sh`).
Start the API: `venv/bin/uvicorn app.main:app --port 8000`. Send prompts to Telegram (`venv/bin/python telegram_bot.py`) or `POST /v1/prompt` with header `X-API-Key`.
Writes always ask first: reply `yes` to execute, `no` to cancel.

## Seeded data

| Type | Records |
|---|---|
| Customers | XYZ Traders, Gulf Interiors LLC, Salalah Hospitality Group, Nizwa Builders, Sohar Tech Park |
| Products (price) | Office Workstation 500, Executive Desk 850, Ergonomic Chair 220, Conference Table (12 seats) 1800, Filing Cabinet 150, Reception Counter 2400, Meeting Room Chair 120, Storage Locker 95 |
| Leads | one per customer, e.g. "Gulf Interiors - Showroom Fitout" (60,000), "Sohar Tech Park - Co-working Furniture" (90,000) |
| Quotations | Gulf Interiors (3 days ago), Salalah (10 days ago), Nizwa (40 days ago); Sohar Tech Park confirmed order (20 days ago) |
| Calendar (tomorrow) | Weekly sales sync 10:00, Lunch with Gulf Interiors 13:00, Site visit - Nizwa Builders 16:00 |

## Ask for information (read-only)

- Which quotations do we have for Gulf Interiors?
- Show all quotations from the last 15 days.
- Which quotations did we send last month?
- List my open leads and their expected revenue.
- What products do you have that are under 200?
- How much is a conference table?
- What does my calendar look like tomorrow?
- When am I free tomorrow afternoon for a 1 hour meeting?

## Create sales orders / quotations (asks to confirm)

- Create a quotation for Nizwa Builders for 4 executive desks and 4 ergonomic chairs.
- Make an order for 10 office workstations for XYZ Traders.
- Quote Salalah Hospitality Group 1 reception counter and 20 meeting room chairs.
- Create a quotation for Sohar Tech Park for 5 conference tables, then reply `yes`.
- Two-turn clarification: `Create a quotation for Gulf Interiors` -> assistant asks what products -> `3 filing cabinets`.

## Create products and sell products that do not exist yet

- Add a new product called Standing Desk Pro, price 1200, electric height adjustable 140cm.
- Add a product Neon Sign, price 85, LED 60cm.
- Sell 2 Standing Desk Pro and 4 Glass Whiteboard at 300 each to Nizwa Builders. (Glass Whiteboard is created together with the order.)
- Create a sale for Sohar Tech Park for 3 Smart Podium at 450 each.
- No price given for a missing product -> the assistant asks for it (it never guesses): `Sell 1 Rooftop Planter to XYZ Traders`.
- Price 0 or a duplicate name is refused: `Add a product Neon Sign at 0`.

## See a customer's sales orders

- Show me all sales orders of Gulf Interiors.
- Show confirmed orders of Sohar Tech Park.
- Show the details of order S00007.

## Misheard or misspelt names (fuzzy matching)

- Show me the sales orders of Nazwa Builders. -> "Did you mean Nizwa Builders?" -> `yes`.
- Create a sale for Soha Tech Park for 2 Smart Podium. -> "Did you mean Sohar Tech Park?" -> `yes` -> preview -> `yes`.
- Sell 1 workstatoin to XYZ Traders. -> asks about "Office Workstation".
- Add a product called Executive Desks, price 900. -> refused as very similar to "Executive Desk"; answer "it is a different product" to override.
- A name with no close match (Totally Unknown Corp) still gets a plain "not found".

## Voice

Send any prompt above as a Telegram voice note. The bot replies "Heard: ..." with the transcript first, then answers; say "yes" as a voice note to confirm.
`GROQ_TRANSCRIBE_MODEL=whisper-large-v3` is recommended (handles Bengali and English); set `GROQ_TRANSCRIBE_LANGUAGE=en` or `bn` to force a language.

## Book calendar

- Book a meeting with Gulf Interiors tomorrow at 11am called Showroom design review.
- Schedule a 45 minute call with XYZ Traders on Monday at 2pm.
- Book "Quarterly review" next Thursday at 10:30 for 1 hour.
- Conflict check (should be refused, suggests another time): Book a meeting tomorrow at 10:15 called Clash test.
- Past check (should be refused): Book a meeting yesterday at 9am.

## Log CRM activity

- I had a call with XYZ Traders today at 11am about the BOQ, log it.
- Log a call on the Sohar Tech Park lead: they want a revised quote for 40 workstations.
- Log a call (missing details -> assistant asks a follow-up).

## Context and safety checks

- `Find Nizwa Builders` then `create a quotation for them for 3 filing cabinets` (remembers the customer).
- Create any quotation, then reply `no` -> nothing is written to Odoo.
- Create a quotation for 99999 workstations -> refused (quantity limit).
- Create a quotation for a customer that does not exist -> assistant asks, does not invent one.
- Try to confirm a quotation with the `telegram` key -> refused; the `admin` key (CONFIRM_CALLERS) may confirm.
- Voice: record any prompt above as a Telegram voice note.
