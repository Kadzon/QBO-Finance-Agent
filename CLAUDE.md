# CLAUDE.md — qbagent

Read this at the start of every session.

## What this is

**qbagent** is an open-source tool that connects to QuickBooks Online and answers financial questions in natural language. It syncs QBO data to a local analytical DB, then an LLM agent translates questions to SQL, executes them, and interprets the results.

Target user: a small-business owner or bookkeeper who wants answers without opening QuickBooks or writing SQL.

**Design priority: frictionless install.** Optimize every decision for `git clone` to first answer in under 10 minutes.

## Architecture

```
CLI / optional FastAPI UI
         │
      Agent (LangGraph)
   retrieve → generate → validate → execute → interpret → log
         │                                         │
         ▼ analytical SQL                          ▼ live lookups
   Local DB (DuckDB/SQLite/Postgres) ◄── sync ── QBO MCP server
                                                    │
                                              QuickBooks Online
```

Two paths to QBO data. The **sync path** pulls QBO → local DB for aggregation-heavy queries. The **live path** calls MCP tools directly for single-record lookups. The agent's router picks which path based on the question.

## Non-negotiables

1. **Read-only in v1.** No writes to QuickBooks. Writes are v2, behind an explicit flag.
2. **No hard-coded LLM provider.** Everything goes through LiteLLM. User brings their own key.
3. **Three backends, one schema.** Generated SQL works on DuckDB, SQLite, and Postgres. Backend-specific code lives behind the `Backend` protocol.
4. **No external services required.** A user with Python and an LLM key must be able to run the full tool. n8n, LangSmith, hosted databases — all optional.
5. **Every query is auditable.** Show the SQL, the result, and the interpretation. Never return a number without its derivation.

## Repo layout

```
src/qbagent/
  cli.py              # typer CLI
  config.py           # env-driven Settings
  llm/provider.py     # LiteLLM wrapper — only place LLM calls live
  db/
    backend.py        # Backend protocol
    schema.sql        # portable DDL
    {duckdb,sqlite,postgres}_backend.py
  sync/
    mcp_client.py     # pluggable MCP client
    sync_runner.py
    mappers/          # one per QBO entity
  agent/
    graph.py          # LangGraph wiring
    nodes.py
    validator.py      # sqlglot-based, no regex on SQL
    memory.py
    prompts.py
    rules/curated.yaml
  server.py           # optional FastAPI
tests/
  golden/             # the correctness contract
```

## Key decisions (with reasons)

- **LiteLLM for LLM calls** — provider-agnostic, one retry/logging path.
- **sqlglot for SQL validation** — AST parsing, not regex. Also handles dialect transpilation across the three backends.
- **MCP for QBO** — replaces a custom API client; gives sync and live-lookup capabilities from the same interface.
- **DuckDB as default backend** — zero-setup, fast analytics on financial data. SQLite and Postgres also supported.
- **Hand-curated rules in v1, automatic rule learning in v2** — auto-learning from user corrections is a minefield; ship human-in-the-loop first.

## Financial domain rules the agent must follow

Baked into the system prompt and the validator's warnings:

- Filter `status != 'Voided'` on invoices, bills, expenses.
- Revenue = `SUM(invoice_lines.amount)` joined to invoices (`status IN ('Sent','Paid')`) and income accounts. Not `invoices.total_amount`.
- Expenses = `SUM(bill_lines.amount) + SUM(expenses.total_amount)`. Do not add `transactions`.
- Outstanding A/R uses `invoices.balance`, not `total_amount`.
- Default to accrual (use `invoice_date` / `bill_date`). Switch to cash only on request.
- Never join `transactions` with `invoices` in the same aggregate — double-counts revenue.
- Every aggregate has a date filter. If the user didn't specify, ask.

## When in doubt

If a design choice would contradict this file, stop and ask. Prefer the smallest correct thing over the most complete one.
