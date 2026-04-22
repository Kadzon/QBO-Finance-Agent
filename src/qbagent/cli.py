"""qbagent command-line interface.

In Phase 0 the subcommands are stubs that announce what they'll do. ``doctor``
is real: it reports the config health and returns a non-zero exit code when a
needed credential is missing.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from qbagent import __version__
from qbagent.config import ConfigError, Settings, get_settings
from qbagent.db.backend import Backend
from qbagent.db.factory import create_backend
from qbagent.llm.provider import LiteLLMProvider, LLMError
from qbagent.sync.mcp_client import CANONICAL_ENTITIES, StdioMCPClient
from qbagent.sync.sync_runner import SyncReport, SyncRunner

app = typer.Typer(
    name="qbagent",
    help="Natural-language QuickBooks Online agent.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
err_console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"qbagent {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
) -> None:
    """Shared CLI options."""


@app.command()
def sync(
    full: Annotated[
        bool, typer.Option("--full", help="Wipe sync state and pull everything.")
    ] = False,
    entity: Annotated[
        str | None,
        typer.Option("--entity", help="Sync only a single entity (e.g. invoices)."),
    ] = None,
) -> None:
    """Pull QuickBooks data into the local analytical DB."""
    settings = get_settings(refresh=True)
    try:
        settings.require_backend()
        settings.require_mcp()
        settings.require_qbo()
    except ConfigError as exc:
        err_console.print(f"[red]config error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    if entity is not None and entity not in CANONICAL_ENTITIES:
        raise typer.BadParameter(
            f"unknown entity {entity!r}. Valid: {', '.join(CANONICAL_ENTITIES)}"
        )
    entities = [entity] if entity else None

    report = asyncio.run(_run_sync(settings, full=full, entities=entities))

    table = Table(title="Sync report", show_header=True, header_style="bold")
    table.add_column("Entity")
    table.add_column("Status")
    table.add_column("Rows")
    table.add_column("Detail", overflow="fold")
    for r in report.results:
        style = {"success": "green", "error": "red", "skipped": "yellow"}.get(r.status, "white")
        detail = r.error or (f"cursor={r.cursor}" if r.cursor else "")
        table.add_row(r.entity, f"[{style}]{r.status}[/{style}]", str(r.rows_synced), detail)
    console.print(table)
    console.print(
        f"Total rows: [bold]{report.total_rows}[/bold]  "
        f"Elapsed: [bold]{(report.finished_at - report.started_at).total_seconds():.2f}s[/bold]"
    )
    if not report.ok:
        raise typer.Exit(code=1)


async def _run_sync(
    settings: Settings,
    *,
    full: bool,
    entities: list[str] | None,
) -> SyncReport:
    backend: Backend = create_backend(settings)
    await backend.connect()
    try:
        await backend.initialize_schema()
        client = StdioMCPClient(settings)
        await client.connect()
        try:
            runner = SyncRunner(client, backend)
            if full:
                return await runner.sync_full(entities)
            return await runner.sync_incremental(entities)
        finally:
            await client.close()
    finally:
        await backend.close()


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="A plain-language question.")],
) -> None:
    """Answer a single question and exit."""
    console.print(f"[dim](stub)[/dim] would answer: {question!r}")


@app.command()
def chat() -> None:
    """Interactive REPL with session persistence."""
    console.print("[dim](stub)[/dim] would start an interactive chat session.")


@app.command()
def doctor(
    test_llm: Annotated[
        bool,
        typer.Option("--test-llm", help="Make a live call to the configured LLM."),
    ] = False,
    init: Annotated[
        bool,
        typer.Option("--init", help="Initialize the DB schema and load curated rules."),
    ] = False,
) -> None:
    """Report config and runtime health."""
    settings = get_settings(refresh=True)
    rows = _collect_doctor_rows(settings)

    table = Table(title="qbagent doctor", show_header=True, header_style="bold")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")

    all_ok = True
    for name, status, detail in rows:
        style = {"ok": "green", "missing": "yellow", "error": "red"}[status]
        marker = {"ok": "OK", "missing": "MISSING", "error": "ERROR"}[status]
        table.add_row(name, f"[{style}]{marker}[/{style}]", detail)
        if status != "ok":
            all_ok = False

    console.print(table)

    if test_llm:
        ok, detail = _probe_llm(settings)
        style = "green" if ok else "red"
        marker = "OK" if ok else "ERROR"
        console.print(f"LLM live call: [{style}]{marker}[/{style}] — {detail}")
        if not ok:
            all_ok = False
    if init:
        console.print("[yellow]--init is not wired up yet; ships in Phase 7.[/yellow]")

    if not all_ok:
        raise typer.Exit(code=1)


def _collect_doctor_rows(settings: Settings) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []

    # LLM
    try:
        settings.require_llm()
        rows.append(("LLM", "ok", f"model={settings.llm_model}"))
    except ConfigError as exc:
        rows.append(("LLM", "missing", str(exc)))

    # Backend (config + live can-connect)
    try:
        settings.require_backend()
    except ConfigError as exc:
        rows.append(("Backend", "error", str(exc)))
    else:
        ok, detail = _probe_backend(settings)
        rows.append(
            (
                "Backend",
                "ok" if ok else "error",
                f"{settings.backend.value} @ {settings.effective_db_target} — {detail}",
            )
        )

    # MCP
    try:
        settings.require_mcp()
        rows.append(("MCP server", "ok", " ".join(settings.mcp_server_argv)))
    except ConfigError as exc:
        rows.append(("MCP server", "missing", str(exc)))

    # QBO credentials
    try:
        settings.require_qbo()
        rows.append(
            (
                "QuickBooks credentials",
                "ok",
                f"realm={settings.qbo_realm_id} env={settings.qbo_environment.value}",
            )
        )
    except ConfigError as exc:
        rows.append(("QuickBooks credentials", "missing", str(exc)))

    # Observability
    rows.append(
        (
            "Logging",
            "ok",
            f"level={settings.log_level} format={settings.log_format}",
        )
    )
    return rows


def _probe_backend(settings: Settings) -> tuple[bool, str]:
    """Try to open the backend, run SELECT 1, and close.

    Returns ``(ok, detail)``. Failure is non-fatal for ``doctor`` — the message
    goes into the detail column so the user can see what broke.
    """

    async def _run() -> str:
        backend = create_backend(settings)
        await backend.connect()
        try:
            await backend.execute_read("SELECT 1")
        finally:
            await backend.close()
        return "connected"

    try:
        detail = asyncio.run(_run())
        return True, detail
    except Exception as exc:
        return False, f"connect failed: {exc}"


def _probe_llm(settings: Settings) -> tuple[bool, str]:
    """Make a tiny live LLM call to confirm auth and connectivity."""
    try:
        settings.require_llm()
    except ConfigError as exc:
        return False, str(exc)

    async def _run() -> str:
        provider = LiteLLMProvider(settings)
        resp = await provider.complete(
            "Reply with exactly one word: PONG.",
            system="You are a health check. Answer literally, one word, no punctuation.",
            max_tokens=8,
            temperature=0.0,
        )
        return resp.content.strip()

    try:
        reply = asyncio.run(_run())
        return True, f"model replied {reply!r}"
    except LLMError as exc:
        return False, f"call failed: {exc}"
    except Exception as exc:
        return False, f"call failed: {exc}"


def main() -> None:
    try:
        app()
    except ConfigError as exc:
        err_console.print(f"[red]config error:[/red] {exc}")
        sys.exit(2)


if __name__ == "__main__":
    main()
