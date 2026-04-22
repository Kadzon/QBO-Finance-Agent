# PLAN.md — qbagent build plan

Work through phases in order. Don't start a phase until the previous phase's exit criteria are met. Read `CLAUDE.md` first — it has the architecture, constraints, and domain rules every phase depends on.

---

## Phase 0 — Scaffolding

Stand up the repo: `pyproject.toml` (Python 3.11+, hatchling), directory layout from `CLAUDE.md`, ruff + mypy (strict) + pytest + pytest-asyncio, pre-commit hooks, GitHub Actions for lint/type/test on 3.11 and 3.12.

Implement `config.py` as a `pydantic-settings` `Settings` object that fails fast on missing required env vars. Create `.env.example` covering: LLM (`QBAGENT_LLM_MODEL`, `QBAGENT_LLM_API_KEY`), backend (`QBAGENT_BACKEND`, `QBAGENT_DB_PATH` or `QBAGENT_DB_URL`), QBO MCP (`QBAGENT_MCP_SERVER_COMMAND`, `QBAGENT_MCP_SERVER_ARGS`, `QUICKBOOKS_*`), optional observability.

Implement `cli.py` with `typer` — stubs for `sync`, `ask`, `chat`, `doctor` that print what they would do.

**Exit:** `pip install -e .` works in a fresh venv. `qbagent --help` lists the commands. `qbagent doctor` reports config status with the right exit code. CI green.

---

## Phase 1 — Database backends

Three backends behind one protocol, same schema, same behavior.

**Schema (`db/schema.sql`):** Portable DDL covering `accounts`, `invoices`, `invoice_lines`, `bills`, `bill_lines`, `expenses`, `transactions`, `sync_log`, `memory_rules`, `query_log`. Constraints for portability: `INTEGER 0/1` instead of `BOOLEAN`, JSON-string columns for arrays, no Postgres schemas, no backend-specific types. Parse-check the DDL on all three engines before proceeding.

**Protocol (`db/backend.py`):** `Backend` defining `connect`, `close`, `initialize_schema`, `execute_read`, `execute_write`, `bulk_upsert(table, rows, pk_column)`, `get_schema_description()`. Plus a `dialect` attribute (`"duckdb"` | `"sqlite"` | `"postgres"`) used by `sqlglot.transpile`.

**Implementations:** `duckdb_backend.py` (duckdb), `sqlite_backend.py` (aiosqlite), `postgres_backend.py` (asyncpg). Raw SQL with parameter binding, no ORM. Each implements its own `bulk_upsert` idiom.

**Tests:** One parameterized test class runs the full suite against all three — schema is idempotent, round-trip preserves DATE/DECIMAL/TIMESTAMP, upsert does the right thing.

**Exit:** All three backends pass the same test suite. `qbagent doctor` reports backend health correctly for whichever is configured.

---

## Phase 2 — LLM provider

One entry point for every LLM call. Built before the agent so every node uses it from day one.

`llm/provider.py` wraps LiteLLM. Expose `complete(prompt, system, max_tokens, temperature, response_format)` and `chat(messages, tools)`. Retries with exponential backoff via `tenacity` on transient errors. Structured logging via structlog: model, token counts, latency, retry count at INFO; prompt/completion contents only at DEBUG (may contain financial data).

Ship a `FakeLLMProvider` test double that returns scripted responses. Every test uses it — tests never hit real APIs.

**Exit:** Unit tests pass. `qbagent doctor --test-llm` successfully calls the real configured provider. Retries engage on simulated failures.

---

## Phase 3 — QBO sync via MCP

**MCP client (`sync/mcp_client.py`):** Uses the official `mcp` Python SDK. Spawns the MCP server as a subprocess via the command from env (so users can swap between Intuit's server, MCPBundles, or others). Exposes `list_entities`, `get_entity`, `query`, `get_changes_since(cursor)`. Handles pagination internally.

**Mappers (`sync/mappers/*.py`):** Pure functions of the form `map_to_rows(qbo_entity) -> {table: [rows]}`. One module per entity: accounts, invoices (→ invoices + invoice_lines), bills (→ bills + bill_lines), expenses, transactions. Tested with recorded MCP response JSON in `tests/fixtures/mcp_responses/`.

**Sync runner (`sync/sync_runner.py`):** `sync_full()` wipes sync state and pulls everything. `sync_incremental()` uses `sync_log.last_cursor` per entity and pulls only changes via the MCP CDC endpoint. Order: accounts first, then invoices/bills/expenses, then transactions. One entity failing doesn't block the others. Updates `sync_log` after each.

**CLI:** `qbagent sync`, `--full`, `--entity <name>`.

**Exit:** Sync against a QBO sandbox populates all tables. Second run is incremental and fast. Five figures spot-checked against the QuickBooks UI match.

---

## Phase 4 — SQL validator

This is the trust boundary between the LLM and the database. `sqlglot` AST only — never regex on SQL.

`agent/validator.py` exposes `validate(sql, dialect, schema_catalog) -> ValidationResult(safe, errors, warnings, normalized_sql)`.

**Layer 1 — Safety (hard reject):** Root must be `Select` or `Union`. Reject any `Insert`/`Update`/`Delete`/`Drop`/`Alter`/`Create`/`Truncate`/`Merge`/`Command` node anywhere in the tree. Reject multiple statements. Block system catalogs (`pg_*`, `information_schema`, `sqlite_master`, `duckdb_*`). Block file/network functions (`read_csv`, `read_parquet`, `httpfs`, `COPY`, `ATTACH`, `LOAD`, `INSTALL`).

**Layer 2 — Scope (hard reject, retriable):** Every `Table` and resolvable `Column` must exist in the schema catalog. Unknown references return to the LLM with a structured error listing available options.

**Layer 3 — Financial correctness (soft warnings):** Pass back to the LLM for one self-correction round, then execute. Implement as pure functions over the AST:
- missing `status != 'Voided'` filter on invoices/bills/expenses
- `SUM(invoices.total_amount)` used for revenue (should be `invoice_lines.amount` + income accounts)
- `transactions` joined with `invoices` in the same aggregate
- aggregations without date filters
- `SELECT` without `LIMIT` or aggregation
- cash-basis questions using accrual dates (or vice versa)

After validation passes, `sqlglot.transpile` from source dialect to target backend dialect. The LLM writes one flavor; the backend runs its own.

**Exit:** ≥95% branch coverage on `validator.py`. A maintained "known bad" list is fully rejected. A "known good" list is fully accepted. Transpilation round-trips golden-set queries across all three dialects.

---

## Phase 5 — Agent graph

`agent/state.py`: `AgentState` TypedDict carrying question, session_id, retrieved rules, generated_sql, validation_result, query_result, final_answer, sanity_warnings, and attempt counters.

`agent/nodes.py`:
- **retrieve_context** — loads schema description + all hand-curated rules from `memory_rules` (no embeddings in v1).
- **generate_sql** — builds a prompt from schema + rules + question (+ previous SQL and errors on retry). Calls LLM, extracts SQL.
- **validate_sql** — calls the validator. Hard errors loop back to generate (max 3 attempts). Warnings trigger one self-correction round.
- **execute_sql** — runs on the backend. Runtime errors loop back to generate with the DB message.
- **interpret_result** — builds a natural-language answer from question + SQL + rows. Runs sanity checks (negative revenue, zero rows when expected non-zero, implausible magnitudes) and populates `sanity_warnings`.
- **log_query** — always runs, writes to `query_log`.

`agent/graph.py`: Wire the nodes. Retry budgets tracked in state; exceeding any budget routes to `interpret_result` with a graceful failure message.

**System prompt (`agent/prompts.py`):** Schema with column-level comments, financial domain rules as an explicit list, loaded memory rules as a section, output format (a delimited SQL block, nothing else). Keep under 8K tokens.

**Tests:** Each node in isolation with fake LLM. Graph-level test walks happy path + retry paths. Golden-set test runs every question, asserts SQL validity and result shape.

**Exit:** Happy-path golden questions produce valid executable SQL. Every failure mode (hard error, runtime error, timeout, empty result) produces a user-facing message, not a stack trace.

---

## Phase 6 — Curated rules + correction capture

**Curated rules:** `agent/rules/curated.yaml` holds 6–10 hand-authored rules covering the financial domain rules from `CLAUDE.md`: voided filter, revenue source, no transactions↔invoices join, outstanding A/R, accrual default, date filter required, expenses composition. Each rule has `id`, `text`, `tags`. `agent/memory.py` upserts them into `memory_rules` with `source='curated'` on first init.

**Correction capture (logging only, no auto-learning):** Detect phrases like "that's wrong," "actually," "you're missing," "exclude." Log the turn with `correction_detected=true` in `query_log`. Print: *"Noted — run `qbagent review-corrections` to turn this into a rule."* A human approves rules in v1; auto-learning is v2.

**Exit:** Curated rules load cleanly. A/B test shows golden-set accuracy is measurably better with rules loaded than without. Correction detection logs correctly without auto-mutating the rules table.

---

## Phase 7 — CLI and optional server

**CLI commands:**
- `doctor` / `doctor --init` — config + backend + LLM + MCP health; `--init` also initializes schema and loads curated rules.
- `sync` / `--full` / `--entity X`
- `ask "<question>"` — one-shot, prints answer + SQL + freshness stamp.
- `chat` — REPL with session persistence via `query_log.session_id`.
- `review-corrections` — interactive: walk recent corrections, offer to turn each into a rule.
- `rules list|add|remove`
- `query-log [--session ID] [--tail N]`

Use `rich` for output. Currency formatted as `$1,234,567.89`. Every answer shows a data-freshness stamp.

**Server (`server.py`):** Optional FastAPI app with `POST /ask`, `GET /sessions/{id}`, `GET /health`, `POST /sync`. Minimal HTML page at `/` for local use. Not production-ready in v1 — document that.

**Exit:** All CLI commands work end-to-end against sandbox QBO. `doctor` gives actionable messages for every misconfiguration. Server boots and handles a question.

---

## Phase 8 — Golden set and CI

The golden set is the correctness contract. It blocks merges on regression.

**`tests/golden/questions.yaml`:** 25+ questions across revenue, expenses, profit, A/R, A/P, cash flow, customer/vendor breakdowns, and trends over time. Each question asserts *shape* (aggregate vs. list, required filters, forbidden joins) — not exact numbers, which drift as sandbox data changes.

**`tests/golden/fixtures/seed.sql`:** Curated tiny dataset (~50 rows) with known answers to ~10 simple questions. A separate golden test asserts exact values against this fixture.

**CI:** Two jobs — shape assertions against a sandbox snapshot, exact-value assertions against the seeded fixture. Regression blocks merge.

**Exit:** 25+ questions defined. Seeded-fixture pass rate 100%. Shape-assertion pass rate ≥90%. CI blocks on regression.

---

## Phase 9 — Documentation and release

**README:** Five-command quick start, env var table, backend tradeoffs, LLM tradeoffs, QBO developer-app registration walkthrough, architecture diagram, troubleshooting. A 90-second demo GIF showing `doctor → sync → ask`.

**`docs/`:** `ARCHITECTURE.md` (contributor-focused deep dive), `WRITING_RULES.md`, `ADDING_A_BACKEND.md`, `ADDING_AN_ENTITY.md`, `SECURITY.md` (threat model, validator scope, bug-report process).

**Release:** Tag v0.1.0, publish to PyPI, GitHub release with honest known limitations.

**Exit:** A first-time user following only the README gets a working answer. Every env var and CLI command is documented.

---

## After v1 (not now)

Tracked separately, for context: automatic rule learning from corrections, write operations, multi-tenant / hosted mode, other sources (Xero, Wave), scheduled syncs, richer web UI with charts.
