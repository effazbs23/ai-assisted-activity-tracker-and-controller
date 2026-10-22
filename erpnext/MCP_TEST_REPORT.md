# Test report: generic ERPNext MCP server (`frappe`)

Date: 2026-10-22. Branch `ai-mcp-erpnext`. Tested end to end through Telegram Web (`https://web.telegram.org/k/`, bot `@bs_artificial_bot`), driven with Claude in Chrome.

## Environment

| Item | Value |
|---|---|
| MCP server tested | `frappe` = `erpnext/mcp_server/frappe_server.py`, stdio, started by the API as `venv/bin/python -m mcp_server.frappe_server` (`MCP_SERVERS` in `.env`) |
| Server protocol | JSON-RPC 2.0 over stdin/stdout, MCP `2024-11-05`, 9 tools; caller sent in `tools/call` `params._meta.caller` |
| Identity | Telegram user `1722472434` maps to caller `manager` (Sales Manager + Item Manager); calls run with `manager`'s own ERPNext API key |
| ERPNext | v15.124.0 (Frappe 15.123.1), local `http://127.0.0.1:8000` |
| API | FastAPI on `127.0.0.1:8100`; Telegram bot `telegram_bot.py` (long polling) |
| LLM | Groq, `openai/gpt-oss-120b` (fallback `gpt-oss-20b`) |
| Python | 3.12.3 |
| Offline suite | 121 passed (`pytest tests --ignore=tests/live`) |

## Telegram prompts and results

| # | Prompt sent | Expected | Result |
|---|---|---|---|
| 1 | `list all warehouses` | Built-in tools cannot; falls back to MCP `list_docs`, runs at once | **Failed first, then passed.** 1st run: "I don't have a tool to retrieve a list of warehouses" (bug 1 below). After the fix: listed All Warehouses, Finished Goods, Goods In Transit, Stores, Work In Progress (all `- AMT`). |
| 2 | `how many employees do we have in the system?` | ERPNext applies `manager`'s roles | **Pass (permission enforced).** "I don't have permission to view the Employee records." ERPNext itself denied `manager` on Employee (confirmed by calling the server directly). |
| 3 | `how many projects do we have?` | `count_docs` | **Not completed.** Groq returned 429, bot said "The assistant is unavailable right now". Server side verified directly: count = 0. |
| 4 | `list all customer groups` | `list_docs` | **Pass** (on retry after the 429): All Customer Groups, Commercial, Government, Individual, Non Profit. |
| 5 | `create a customer group called Test Group MCP under All Customer Groups` | Write is parked until yes | **Failed first, then passed.** 1st run: the model only asked "Do you want me to create...?" in text, nothing pending (bug 2). After the prompt fix: pending action created (raw JSON echoed, bug 3). |
| 5b | `yes` | Runs as `manager` | **Pass (permission enforced).** "I could not do that: MCP tool frappe.create_doc failed: ERPNext denied access: User manager@example.com does not have doctype access ... Customer Group". Nothing was created. |
| 6 | `yes` (sent while nothing was pending, after 5's first run) | Must not create anything | **Pass.** Nothing was created (this request also hit a 429). |
| 7 | `create a note titled MCP test note with the content hello from telegram` | Clean preview, nothing written before yes | **Pass.** "I will run frappe.create_doc with {...} Please reply yes to confirm or no to cancel." ERPNext check before yes: no such Note. |
| 7b | `yes` | Note created as `manager` | **Pass.** "Done. MCP tool frappe.create_doc ran." Verified in ERPNext: Note `vmckt49vus`, owner `manager@example.com`, content "hello from telegram". |
| 8 | `create a note titled Cancel me with the content should not exist` | Preview | **Pass.** |
| 8b | `no` | Nothing written | **Pass.** "Cancelled. Nothing was changed in ERPNext." |
| 9 | `list all users and their roles` | Security DocType blocked | **Not completed via Telegram** (Groq 429 on every retry, both models). Verified at server level instead (next table). |

## Direct server checks (no LLM, real ERPNext, per caller)

| Call | manager | alice | bob |
|---|---|---|---|
| `count_docs` Warehouse | 5 | 5 | 5 |
| `count_docs` Customer | 6 | 6 | 6 |
| `list_docs` User | blocked (`PermissionError: User is not available`) | blocked | blocked |
| `cancel_doc` Sales Order | passes the guard (ERPNext: record not found) | blocked (`Only authorised callers can submit or cancel Sales Orders`) | blocked |
| `list_docs` with no `_meta.caller` | refused: `No caller identity was supplied` | | |

## Bugs found while testing, and fixes

1. **Fallback never started** (`orchestrator.py`, `UNABLE`): the model writes a curly apostrophe (`don’t`), the regex only matched `don't`. Fixed to accept both; test `test_curly_apostrophe_still_counts_as_unable` added.
2. **Model asked for confirmation in text instead of proposing**: phase-2 prompt allowed "ask one short clarifying question". Prompt now says to call the write tool right away and never ask in text whether to proceed.
3. **Raw tool JSON shown to the user** for a pending MCP write: the reply is now built by code ("I will run frappe.create_doc with {...} Please reply yes to confirm or no to cancel.").
4. **Verbose result after yes**: the saved record came back as a full JSON dump (seen in test 7b); now compacted to "Note <name> saved." This change was made after test 7b and was covered by the offline suite only, not re-run through Telegram.

## Not covered / caveats

- Groq `on_demand` rate limits (429, both models) blocked prompts 3 and 9 and slowed the rest; this is an environment limit, not an MCP failure. Each phase-2 request carries the full tool list plus history, which is heavy for the token budget.
- The positive write path was proven with a Note, because `manager` cannot create Customer Groups. Submit/cancel and `run_report`/`describe_doctype` were not exercised live.
- Only the `manager` caller is linked to a Telegram account, so `alice`/`bob` were tested at the server level only.
- Test data: the Note `vmckt49vus` was deleted again after verification; nothing else was created.
- Nothing is committed. `telegram_bot.py` still carries the earlier uncommitted change.
