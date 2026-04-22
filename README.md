# qbagent

Natural-language agent for QuickBooks Online. Ask questions like *"what was our revenue last quarter?"* and get an answer backed by auditable SQL.

**Status:** Phase 0 scaffolding. Not yet functional — the CLI commands are stubs. See [PLAN.md](PLAN.md) for the build plan and [CLAUDE.md](CLAUDE.md) for the architecture and non-negotiables.

## Quick start

```bash
git clone https://github.com/qbagent/qbagent
cd qbagent
python -m venv .venv && source .venv/bin/activate  # on Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env   # then edit with your LLM + QBO credentials
qbagent doctor
```

Once the later phases land, the full loop will be:

```bash
qbagent doctor --init   # checks config, initializes the local DB
qbagent sync            # pulls your QuickBooks data
qbagent ask "what was revenue last quarter?"
```

## Design priorities

- **Frictionless install.** `git clone` to first answer in under 10 minutes.
- **Read-only in v1.** No writes to QuickBooks.
- **Bring your own LLM.** Anything LiteLLM supports (Anthropic, OpenAI, Azure, local).
- **Three backends.** DuckDB (default), SQLite, or Postgres — same schema everywhere.
- **Every query is auditable.** SQL, result, and interpretation are always visible.

## Development

```bash
pip install -e ".[dev]"
pre-commit install
ruff check . && ruff format --check .
mypy
pytest
```

## License

MIT.
