"""OpenAgent CLI (spec §32).

The automation surface. Every command goes through the same service layer the TUI uses. Secrets are
never accepted as command arguments (spec §30): keys are prompted with hidden input or referenced
from an environment variable.
"""

from __future__ import annotations

import asyncio
from typing import NoReturn

import typer
from rich.console import Console
from rich.table import Table

from .. import __version__
from ..app import OpenAgentApp
from ..core.events import NormalizedEvent
from ..core.models import Protocol, RuntimeType, enum_value
from ..core.permissions import profile_names
from ..providers.factory import PRESETS, preset_names
from ..services.agent_service import AgentError
from ..services.provider_service import ProviderInUseError, ProviderValidationError
from ..services.run_service import RunError
from ..tui.markup import safe_line, safe_markup

app = typer.Typer(
    add_completion=False, no_args_is_help=False,
    help="OpenAgent — local-first control plane for AI APIs, coding CLIs, and agents.",
)
provider_app = typer.Typer(help="Manage API provider connections.")
agent_app = typer.Typer(help="Manage agents.")
app.add_typer(provider_app, name="provider")
app.add_typer(agent_app, name="agent")

console = Console()
err = Console(stderr=True)


def _app() -> OpenAgentApp:
    return OpenAgentApp.create()


def _run(coro):
    return asyncio.run(coro)


def _fail(message: str) -> NoReturn:
    # Error messages routinely carry user/model-derived names (provider, agent, model ids); escape so
    # a crafted name can't forge or corrupt the terminal output (item 12).
    err.print(f"[red]error:[/red] {safe_markup(message)}")
    raise typer.Exit(1)


# --------------------------------------------------------------------------- top-level


@app.command()
def version() -> None:
    """Print the OpenAgent version."""
    console.print(f"openagent {__version__}")


@app.command()
def init() -> None:
    """Initialize OpenAgent state for this project."""
    oa = _app()
    console.print("[green]✓[/green] OpenAgent initialized")
    console.print(f"  data:    {oa.paths.data_dir}")
    console.print(f"  db:      {oa.paths.db_path}")
    console.print(f"  project: {oa.paths.project_root}")
    console.print("\nNext: [bold]openagent discover[/bold] to detect CLIs, "
                  "or [bold]openagent provider add[/bold] to connect an API.")


@app.command("discover")
def discover() -> None:
    """Detect installed coding CLIs (spec §32)."""
    oa = _app()
    installs = _run(oa.clis.discover(persist=True))
    known = set(oa.clis.known_types())
    found = {i.type for i in installs}
    for cli_type in sorted(known):
        install = next((i for i in installs if i.type == cli_type), None)
        if install:
            mark = "[green]✓[/green]"
            auth = "authenticated" if install.authenticated else "not authenticated"
            detail = safe_markup(install.version or install.executable)
            console.print(f"{mark} {cli_type} CLI found — {detail} ({auth})")
        else:
            console.print(f"[red]✗[/red] {cli_type} CLI not found")
    for extra in sorted(found - known):  # pragma: no cover
        console.print(f"[green]✓[/green] {extra}")


@app.command("add")
def add_agent(
    name: str = typer.Option(..., "--name", help="Unique agent name."),
    title: str = typer.Option("", "--title"),
    description: str = typer.Option("", "--description"),
    provider: str | None = typer.Option(None, "--provider", help="Provider name (API agent)."),
    model: str | None = typer.Option(None, "--model", help="Model id/label for API or CLI agents."),
    cli: str | None = typer.Option(None, "--cli", help="CLI type, e.g. codex/claude (CLI agent)."),
    tag: list[str] = typer.Option([], "--tag", help="Repeatable tag."),
    system_prompt: str = typer.Option("", "--system-prompt"),
    profile: str = typer.Option("safe-edit", "--profile", help=f"One of: {', '.join(profile_names())}"),
) -> None:
    """Add an agent (API or CLI). Shortcut for `agent add`."""
    oa = _app()
    try:
        if cli:
            # A CLI agent may pin a model too (``codex -m`` / ``claude --model`` / ``agy --model``);
            # ``None`` means "use the CLI's own default". This path used to silently drop --model,
            # so a CLI agent could never be created with a pinned model from the CLI (item 10).
            agent = oa.agents.create(
                name=name, title=title, description=description, runtime_type=RuntimeType.CLI,
                cli=cli, model=model, tags=tag, system_prompt=system_prompt,
                permission_profile=profile,
            )
        else:
            if not oa.providers.get(provider or ""):
                _fail(f"provider {provider!r} not found. Add it first: openagent provider add {provider} --type <type>")
            agent = oa.agents.create(
                name=name, title=title, description=description, runtime_type=RuntimeType.API_AGENT,
                provider=provider, model=model, tags=tag, system_prompt=system_prompt,
                permission_profile=profile,
            )
    except AgentError as exc:
        _fail(str(exc))
    console.print(f"[green]✓[/green] agent [bold]{safe_markup(agent.name)}[/bold] created; "
                  "OPENAGENT.md updated")


@app.command("list")
def list_agents(json_out: bool = typer.Option(False, "--json")) -> None:
    """List agents (alias for `agent list`)."""
    _print_agents(_app(), json_out)


@app.command("runs")
def runs(limit: int = typer.Option(20, "--limit")) -> None:
    """List recent runs."""
    oa = _app()
    table = Table("ID", "Agent", "Status", "Started", "Files")
    for run in oa.runs.list(limit):
        status = enum_value(run.status)
        table.add_row(safe_line(run.id), safe_line(run.agent), safe_line(status),
                      run.started_at.strftime("%m-%d %H:%M"), str(len(run.files_changed)))
    console.print(table)


@app.command()
def run(
    name: str = typer.Option(..., "--name", help="Agent name."),
    prompt: str = typer.Option(..., "--prompt", "-p"),
    worktree: str = typer.Option("auto", "--worktree", help="auto | none | copy"),
    profile: str | None = typer.Option(None, "--profile"),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Approve high-risk operations non-interactively (records approval events).",
    ),
) -> None:
    """Run an agent on a task (spec §32).

    Approvals: without --yes a non-interactive run denies high-risk operations by default.
    """
    oa = _app()
    oa.runs.recover_orphans()
    try:
        run_obj = oa.runs.create(agent_name=name, prompt=prompt, worktree=worktree,
                                 permission_profile=profile, confirm_in_place=yes)
    except RunError as exc:
        _fail(str(exc))
    console.print(f"[dim]run {run_obj.id} starting…[/dim]")
    # A non-interactive CLI has no human to prompt: --yes approves, otherwise deny (never silent).
    approval = (lambda _req: True) if yes else None
    result = _run(oa.runs.execute(run_obj, on_event=_print_event, approval_callback=approval))
    status = enum_value(result.status)
    color = "green" if status == "completed" else "red"
    files = safe_markup(", ".join(result.files_changed) or "(none)")
    console.print(f"\n[{color}]● {status}[/{color}] — run {safe_markup(result.id)}")
    console.print(f"  files changed: {files}")
    console.print(f"  output: [bold]openagent output --id {safe_markup(result.id)}[/bold]")


@app.command()
def output(
    id: str = typer.Option(..., "--id", help="Run id."),
    format: str = typer.Option("md", "--format", help="md|json|diff|logs|events|handoff|status|tests"),
) -> None:
    """Print a run artifact (spec §32)."""
    oa = _app()
    try:
        artifact = oa.runs.output(id, format)
    except RunError as exc:
        _fail(str(exc))
    # Emit the artifact **verbatim**. ``console.print`` soft-wraps at the console width (80 when
    # piped), which injects newlines mid-string and corrupts machine-readable formats — the exact
    # `openagent output --id <run-id> --format json` call OPENAGENT.md tells AI assistants to parse.
    typer.echo(artifact)


@app.command()
def message(
    id: str = typer.Option(..., "--id"),
    prompt: str = typer.Option(..., "--prompt", "-p"),
) -> None:
    """Continue a run's session with a new prompt (spec §32)."""
    oa = _app()
    try:
        result = _run(oa.runs.resume(id, prompt, on_event=_print_event))
    except RunError as exc:
        _fail(str(exc))
    status = enum_value(result.status)
    console.print(f"\n[green]●[/green] {status} — run {result.id}")


@app.command()
def resume(id: str = typer.Option(..., "--id"), prompt: str = typer.Option("continue", "--prompt", "-p")) -> None:
    """Resume a run (spec §32)."""
    message(id=id, prompt=prompt)


@app.command()
def cancel(id: str = typer.Option(..., "--id")) -> None:
    """Cancel a running run (spec §32)."""
    _run(_app().runs.cancel(id))
    console.print(f"[yellow]cancelled[/yellow] {id}")


@app.command()
def doctor(json_out: bool = typer.Option(False, "--json")) -> None:
    """Run system diagnostics (spec §41)."""
    oa = _app()
    checks = _run(oa.doctor.run())
    if json_out:
        console.print_json(data={"checks": [c.to_dict() for c in checks]})
        return
    marks = {"ok": "[green]✓[/green]", "warn": "[yellow]⚠[/yellow]", "fail": "[red]✗[/red]"}
    for check in checks:
        console.print(f"{marks.get(check.status, '?')} {safe_markup(check.name)}"
                      + (f" — [dim]{safe_markup(check.detail)}[/dim]" if check.detail else ""))


@app.command("mcp")
def mcp(action: str = typer.Argument("serve")) -> None:
    """MCP server (planned for v0.3)."""
    _fail("`openagent mcp serve` arrives in v0.3; not available in v0.1.")


# --------------------------------------------------------------------------- provider


@provider_app.command("add")
def provider_add(
    name: str = typer.Argument(..., help="Connection name, e.g. deepseek-main."),
    type: str = typer.Option(..., "--type", help=f"Provider type: {', '.join(preset_names())}"),
    protocol: str | None = typer.Option(None, "--protocol", help="openai-chat|openai-responses|anthropic-messages"),
    base_url: str | None = typer.Option(None, "--base-url"),
    region: str | None = typer.Option(None, "--region"),
    workspace_id: str | None = typer.Option(None, "--workspace-id"),
    key_env: str | None = typer.Option(None, "--key-env", help="Reference an env var instead of storing a key."),
    no_key: bool = typer.Option(False, "--no-key", help="Local provider needs no key (e.g. ollama)."),
) -> None:
    """Register an API provider. The key is prompted with hidden input (never passed as an argument)."""
    oa = _app()
    if oa.providers.get(name):
        _fail(f"provider {name!r} already exists")
    credential_source = "none" if no_key else ("env" if key_env else "keychain")
    api_key = None
    if credential_source == "keychain":
        api_key = typer.prompt(f"API key for {name}", hide_input=True)
    proto = Protocol(protocol) if protocol else None
    try:
        provider = oa.providers.add(
            name=name, provider_type=type, protocol=proto, base_url=base_url,
            api_key=api_key, key_env=key_env, credential_source=credential_source,
            region=region, workspace_id=workspace_id,
        )
    except ProviderValidationError as exc:
        _fail(str(exc))
    console.print(f"[green]✓[/green] provider [bold]{safe_markup(name)}[/bold] added "
                  f"({safe_markup(provider.provider_type)}, {safe_markup(provider.protocol.value)})")
    console.print(f"  test it: [bold]openagent provider test {safe_markup(name)}[/bold]")


@provider_app.command("list")
def provider_list(json_out: bool = typer.Option(False, "--json")) -> None:
    oa = _app()
    providers = oa.providers.list()
    if json_out:
        console.print_json(data=[p.model_dump(mode="json") for p in providers])
        return
    table = Table("Name", "Type", "Protocol", "Base URL", "Key")
    for p in providers:
        cred = p.credential.type if isinstance(p.credential.type, str) else p.credential.type.value
        table.add_row(safe_line(p.name), safe_line(p.provider_type), safe_line(p.protocol.value),
                      safe_line(p.base_url or "(preset)"), safe_line(cred))
    console.print(table)


@provider_app.command("test")
def provider_test(name: str = typer.Argument(...)) -> None:
    oa = _app()
    result = _run(oa.providers.test(name))
    if result.ok:
        console.print(f"[green]✓[/green] {safe_markup(name)}: {safe_markup(result.detail)}")
    else:
        _fail(f"{name}: {result.detail}")


@provider_app.command("models")
def provider_models(name: str = typer.Argument(...)) -> None:
    oa = _app()
    models = _run(oa.providers.remote_models(name))
    if not models:
        console.print("[yellow]no models returned (provider may lack a /models endpoint)[/yellow]")
        return
    for m in models:
        console.print(f"  {safe_markup(m.id)}")


@provider_app.command("remove")
def provider_remove(name: str = typer.Argument(...)) -> None:
    try:
        removed = _app().providers.remove(name)
    except ProviderInUseError as exc:
        _fail(str(exc))
    if removed:
        console.print(f"[green]✓[/green] removed provider {safe_markup(name)}")
    else:
        _fail(f"provider {name!r} not found")


@provider_app.command("presets")
def provider_presets() -> None:
    """List built-in provider presets (spec §12–§24)."""
    table = Table("Type", "Label", "Protocol", "Needs key")
    for preset in PRESETS.values():
        table.add_row(preset.provider_type, preset.label, preset.protocol.value,
                      "yes" if preset.needs_key else "no")
    console.print(table)


# --------------------------------------------------------------------------- agent


@agent_app.command("add")
def agent_add(
    name: str = typer.Option(..., "--name"),
    title: str = typer.Option("", "--title"),
    description: str = typer.Option("", "--description"),
    provider: str | None = typer.Option(None, "--provider"),
    model: str | None = typer.Option(None, "--model", help="Model id/label for API or CLI agents."),
    cli: str | None = typer.Option(None, "--cli"),
    tag: list[str] = typer.Option([], "--tag"),
    system_prompt: str = typer.Option("", "--system-prompt"),
    profile: str = typer.Option("safe-edit", "--profile"),
) -> None:
    """Add an agent (same as top-level `add`)."""
    add_agent(name=name, title=title, description=description, provider=provider, model=model,
              cli=cli, tag=tag, system_prompt=system_prompt, profile=profile)


@agent_app.command("list")
def agent_list(json_out: bool = typer.Option(False, "--json")) -> None:
    _print_agents(_app(), json_out)


@agent_app.command("show")
def agent_show(name: str = typer.Argument(...)) -> None:
    agent = _app().agents.get(name)
    if not agent:
        _fail(f"agent {name!r} not found")
    console.print_json(data=agent.model_dump(mode="json"))


@agent_app.command("remove")
def agent_remove(name: str = typer.Argument(...)) -> None:
    if _app().agents.remove(name):
        console.print(f"[green]✓[/green] removed agent {safe_markup(name)}; OPENAGENT.md updated")
    else:
        _fail(f"agent {name!r} not found")


# --------------------------------------------------------------------------- helpers


def _print_agents(oa: OpenAgentApp, json_out: bool) -> None:
    agents = oa.agents.list()
    if json_out:
        console.print_json(data=[a.model_dump(mode="json") for a in agents])
        return
    table = Table("Name", "Title", "Runtime", "Tags", "Profile")
    for a in agents:
        rt = a.runtime
        rtype = rt.type if isinstance(rt.type, str) else rt.type.value
        runtime = f"{rt.cli}-cli" if rtype == "cli" else f"api:{rt.provider}"
        table.add_row(safe_line(a.name), safe_line(a.title or "—"), safe_line(runtime),
                      safe_line(", ".join(a.tags) or "—"), safe_line(a.permission_profile))
    console.print(table)


def _print_event(event: NormalizedEvent) -> None:
    etype = event.type if isinstance(event.type, str) else event.type.value
    data = event.data
    # Every value below is model- or command-controlled (a tool name, a shell command, a file path, a
    # failure message). Escape each before it enters a Rich markup string, or a payload like
    # "[green]✓ done[/green]" would forge a success line or corrupt the render (item 12).
    tool = safe_markup(data.get("tool", ""))
    command = safe_markup(data.get("command", ""))
    path = safe_markup(data.get("path", ""))
    message = safe_markup(data.get("message", ""))
    icons = {
        "run.started": "[dim]▶ run started[/dim]",
        "tool.requested": f"[cyan]→[/cyan] {tool}",
        "tool.completed": f"[green]✓[/green] {tool}",
        "tool.failed": f"[red]✗[/red] {tool}",
        "command.started": f"[blue]$[/blue] {command}",
        "file.created": f"[green]+[/green] {path}",
        "file.modified": f"[yellow]✎[/yellow] {path}",
        "file.deleted": f"[red]-[/red] {path}",
        "test.completed": f"[magenta]tests[/magenta] {'passed' if data.get('passed') else 'failed'}",
        "run.completed": "[green]● completed[/green]",
        "run.failed": f"[red]● failed[/red] {message}",
    }
    line = icons.get(etype)
    if line:
        console.print("  " + line, highlight=False)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
