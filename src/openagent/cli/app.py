"""OpenAgent CLI (spec §32).

The automation surface. Every command goes through the same service layer the TUI uses. Secrets are
never accepted as command arguments (spec §30): keys are prompted with hidden input or referenced
from an environment variable.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console
from rich.table import Table

from .. import __version__
from ..app import OpenAgentApp
from ..core.events import NormalizedEvent
from ..core.models import Protocol, RuntimeType, enum_value
from ..core.permissions import profile_names
from ..providers.discovery import (
    PROBE_NOT_FOUND,
    PROBE_RATE_LIMITED,
    PROBE_UNAUTHORIZED,
    PROBE_VERIFIED,
    filter_models,
    looks_non_chat,
)
from ..providers.factory import PRESETS, get_preset, preset_names
from ..services.agent_service import AgentError
from ..services.project_service import ProjectError
from ..services.provider_service import ProviderInUseError, ProviderValidationError
from ..services.run_service import CancelOutcome, RunError
from ..tui.markup import safe_line, safe_markup

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="OpenAgent — local-first control plane for AI APIs, coding CLIs, and agents.",
)
provider_app = typer.Typer(help="Manage API provider connections.")
agent_app = typer.Typer(help="Manage agents.")
events_app = typer.Typer(help="Inspect and repair event exports.")
project_app = typer.Typer(help="Manage stable project identities.")
app.add_typer(provider_app, name="provider")
app.add_typer(agent_app, name="agent")
app.add_typer(events_app, name="events")
app.add_typer(project_app, name="project")

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


def emit_json(data: object) -> None:
    """Write one complete machine-readable value directly to stdout."""

    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


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
    console.print(
        "\nNext: [bold]openagent discover[/bold] to detect CLIs, "
        "or [bold]openagent provider add[/bold] to connect an API."
    )


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
    profile: str = typer.Option(
        "safe-edit", "--profile", help=f"One of: {', '.join(profile_names())}"
    ),
    allow_unverified_model: bool = typer.Option(
        False,
        "--allow-unverified-model",
        help="Create the agent even though its model has no verified capability probe.",
    ),
    model_override_reason: str | None = typer.Option(
        None,
        "--model-override-reason",
        help="Required reason when overriding model verification.",
    ),
    reasoning_effort: str | None = typer.Option(None, "--reasoning-effort"),
) -> None:
    """Add an agent (API or CLI). Shortcut for `agent add`."""
    oa = _app()
    try:
        if cli:
            # A CLI agent may pin a model too (``codex -m`` / ``claude --model`` / ``agy --model``);
            # ``None`` means "use the CLI's own default". This path used to silently drop --model,
            # so a CLI agent could never be created with a pinned model from the CLI (item 10).
            agent = oa.agents.create(
                name=name,
                title=title,
                description=description,
                runtime_type=RuntimeType.CLI,
                cli=cli,
                model=model,
                tags=tag,
                system_prompt=system_prompt,
                permission_profile=profile,
                reasoning_effort=reasoning_effort,
            )
        else:
            if not oa.providers.get(provider or ""):
                _fail(
                    f"provider {provider!r} not found. Add it first: openagent provider add {provider} --type <type>"
                )
            if allow_unverified_model and not (model_override_reason or "").strip():
                _fail("--allow-unverified-model requires --model-override-reason")
            _require_verified_model(
                oa,
                provider or "",
                model or "",
                bool((model_override_reason or "").strip()),
            )
            agent = oa.agents.create(
                name=name,
                title=title,
                description=description,
                runtime_type=RuntimeType.API_AGENT,
                provider=provider,
                model=model,
                tags=tag,
                system_prompt=system_prompt,
                permission_profile=profile,
                reasoning_effort=reasoning_effort,
                model_override_reason=model_override_reason,
            )
    except AgentError as exc:
        _fail(str(exc))
    console.print(
        f"[green]✓[/green] agent [bold]{safe_markup(agent.name)}[/bold] created; "
        "OPENAGENT.md updated"
    )
    if model_override_reason and not cli:
        console.print(
            "[yellow]⚠ model verification OVERRIDDEN; this agent is not shown as Verified. "
            f"Reason: {safe_markup(model_override_reason)}[/yellow]"
        )


def _require_verified_model(
    oa: OpenAgentApp, provider: str, model: str, allow_unverified: bool
) -> None:
    """Refuse to create an agent on an unvalidated model from a **mixed catalog** (spec §17.5).

    Scoped to providers whose catalog mixes model types (``catalog_is_mixed`` — NVIDIA Build): there,
    a model id proves nothing, so creating an agent on an unprobed entry would produce an agent that
    silently cannot run. Only a *cached* probe is consulted — an expensive provider call is never made
    silently behind the user's back; the user is told the exact command to run.
    """

    if allow_unverified:
        return
    preset = get_preset(_provider_type(oa, provider))
    if preset is None or not preset.catalog_is_mixed:
        return
    probe = oa.providers.cached_probe(provider, model)
    if probe is None:
        _fail(
            f"model {model!r} has not been validated; run:\n"
            f"  openagent provider probe {provider} --model {model}\n"
            "then re-run this command, or pass --allow-unverified-model to create it anyway."
        )
    if probe.category != PROBE_VERIFIED:
        _fail(
            f"model {model!r} is not verified agent-compatible ({probe.category}): {probe.message()}\n"
            "Choose another model, or pass --allow-unverified-model to create it anyway."
        )


@app.command("list")
def list_agents(json_out: bool = typer.Option(False, "--json")) -> None:
    """List agents (alias for `agent list`)."""
    _print_agents(_app(), json_out)


@app.command("runs")
def runs(
    limit: int = typer.Option(20, "--limit"),
    json_out: bool = typer.Option(False, "--json"),
    all_projects: bool = typer.Option(False, "--all-projects"),
) -> None:
    """List recent runs."""
    oa = _app()
    recent = oa.runs.list(limit, all_projects=all_projects)
    if json_out:
        emit_json([run.model_dump(mode="json") for run in recent])
        return
    table = Table("ID", "Agent", "Status", "Started", "Files")
    for run in recent:
        status = enum_value(run.status)
        table.add_row(
            safe_line(run.id),
            safe_line(run.agent),
            safe_line(status),
            run.started_at.strftime("%m-%d %H:%M"),
            str(len(run.files_changed)),
        )
    console.print(table)


@app.command()
def run(
    name: str = typer.Option(..., "--name", help="Agent name."),
    prompt: str = typer.Option(..., "--prompt", "-p"),
    worktree: str = typer.Option("auto", "--worktree", help="auto | none | copy"),
    profile: str | None = typer.Option(None, "--profile"),
    execution_backend: str = typer.Option(
        "host-restricted", "--execution-backend", help="host-restricted | container-sandbox"
    ),
    container_runtime: str | None = typer.Option(
        None, "--container-runtime", help="docker | podman (auto-detected when omitted)"
    ),
    container_image: str | None = typer.Option(
        None, "--container-image", help="Required local image for container-sandbox"
    ),
    commit_agent_changes: bool = typer.Option(False, "--commit-agent-changes"),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Approve high-risk operations non-interactively (records approval events).",
    ),
) -> None:
    """Run an agent on a task (spec §32).

    Approvals: without --yes a non-interactive run denies high-risk operations by default.
    """
    oa = _app()
    oa.runs.recover_orphans()
    try:
        run_obj = oa.runs.create(
            agent_name=name,
            prompt=prompt,
            worktree=worktree,
            permission_profile=profile,
            confirm_in_place=yes,
            execution_backend=execution_backend,
            container_runtime=container_runtime,
            container_image=container_image,
            commit_agent_changes=commit_agent_changes,
        )
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
    format: str = typer.Option(
        "md", "--format", help="md|json|diff|logs|events|handoff|status|tests"
    ),
    all_projects: bool = typer.Option(False, "--all-projects"),
) -> None:
    """Print a run artifact (spec §32)."""
    oa = _app()
    try:
        artifact = oa.runs.output(id, format, all_projects=all_projects)
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
    all_projects: bool = typer.Option(False, "--all-projects"),
) -> None:
    """Continue a run's session with a new prompt (spec §32)."""
    oa = _app()
    try:
        result = _run(oa.runs.resume(id, prompt, on_event=_print_event, all_projects=all_projects))
    except RunError as exc:
        _fail(str(exc))
    status = enum_value(result.status)
    console.print(f"\n[green]●[/green] {status} — run {result.id}")


@app.command()
def resume(
    id: str = typer.Option(..., "--id"),
    prompt: str = typer.Option("continue", "--prompt", "-p"),
    all_projects: bool = typer.Option(False, "--all-projects"),
) -> None:
    """Resume a run (spec §32)."""
    message(id=id, prompt=prompt, all_projects=all_projects)


@app.command("rerun")
def rerun_command(
    id: str = typer.Option(..., "--id"),
    all_projects: bool = typer.Option(False, "--all-projects"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Run the same request under a new run id."""

    oa = _app()
    try:
        new_run = oa.runs.rerun(id, all_projects=all_projects, confirm_in_place=yes)
    except RunError as exc:
        _fail(str(exc))
    console.print(f"[dim]rerun {new_run.id} starting (from {safe_markup(id)})…[/dim]")
    approval = (lambda _request: True) if yes else None
    result = _run(oa.runs.execute(new_run, on_event=_print_event, approval_callback=approval))
    console.print(
        f"[bold]{safe_markup(enum_value(result.status))}[/bold] — {safe_markup(result.id)}"
    )


@app.command("revert")
def revert_command(
    id: str = typer.Option(..., "--id"),
    all_projects: bool = typer.Option(False, "--all-projects"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Revert the optional agent commit in its owned worktree."""

    try:
        commit = _app().runs.revert_agent_commit(id, all_projects=all_projects)
    except RunError as exc:
        _fail(str(exc))
    if json_out:
        emit_json({"run_id": id, "revert_commit": commit})
    else:
        console.print(f"[green]✓[/green] reverted {safe_markup(id)} as {safe_markup(commit)}")


@app.command()
def cancel(
    id: str = typer.Option(..., "--id"),
    all_projects: bool = typer.Option(False, "--all-projects"),
) -> None:
    """Cancel a running run (spec §32, §3.3).

    Reports exactly what happened and never claims a false success: a run that was already finished,
    or an orphaned run whose recorded process is gone/reused, is reported as such (non-zero exit for
    the cases where nothing was stopped).
    """
    outcome = _run(_app().runs.cancel(id, all_projects=all_projects))
    safe_id = safe_markup(id)
    if outcome is CancelOutcome.TERMINATED:
        console.print(f"[yellow]cancelled[/yellow] {safe_id} — process tree terminated")
    elif outcome is CancelOutcome.SIGNALLED:
        console.print(
            f"[yellow]cancelling[/yellow] {safe_id} — the active turn was signalled to stop"
        )
    elif outcome is CancelOutcome.ALREADY_TERMINAL:
        console.print(f"[dim]{safe_id} has already finished; nothing to cancel[/dim]")
    elif outcome is CancelOutcome.NOT_FOUND:
        _fail(f"run {id!r} not found")
    elif outcome is CancelOutcome.WRONG_PROJECT:
        _fail(f"{id}: run belongs to another project; pass --all-projects explicitly")
    elif outcome is CancelOutcome.ALREADY_GONE:
        _fail(f"{id}: the recorded process has already exited; the run was left untouched.")
    elif outcome is CancelOutcome.IDENTITY_UNKNOWN:
        _fail(
            f"{id}: the recorded process identity is incomplete or cannot be verified; refused to "
            "signal it. The run was left untouched."
        )
    elif outcome is CancelOutcome.IDENTITY_MISMATCH:
        _fail(
            f"{id}: the PID now belongs to a different executable or command; refused to terminate an "
            "unrelated process. The run was left untouched."
        )
    elif outcome is CancelOutcome.ACCESS_DENIED:
        _fail(f"{id}: access was denied while terminating the process tree; state was not changed.")
    elif outcome is CancelOutcome.SURVIVORS_REMAINING:
        _fail(
            f"{id}: one or more process-tree members survived termination; state was not changed."
        )
    elif outcome is CancelOutcome.TERMINATION_FAILED:
        _fail(f"{id}: process-tree termination failed; state was not changed.")
    elif outcome is CancelOutcome.NOT_CANCELLABLE:
        _fail(f"{id}: orphaned run has no safely identifiable live process to cancel.")


@app.command()
def doctor(json_out: bool = typer.Option(False, "--json")) -> None:
    """Run system diagnostics (spec §41)."""
    oa = _app()
    checks = _run(oa.doctor.run())
    if json_out:
        emit_json({"checks": [c.to_dict() for c in checks]})
        return
    marks = {"ok": "[green]✓[/green]", "warn": "[yellow]⚠[/yellow]", "fail": "[red]✗[/red]"}
    for check in checks:
        console.print(
            f"{marks.get(check.status, '?')} {safe_markup(check.name)}"
            + (f" — [dim]{safe_markup(check.detail)}[/dim]" if check.detail else "")
        )


@events_app.command("repair")
def events_repair(
    id: str = typer.Option(..., "--id"),
    json_out: bool = typer.Option(False, "--json"),
    all_projects: bool = typer.Option(False, "--all-projects"),
) -> None:
    """Regenerate JSONL from the authoritative SQLite event bodies."""

    try:
        result = _app().runs.repair_event_export(id, all_projects=all_projects)
    except RunError as exc:
        _fail(str(exc))
    if json_out:
        emit_json(result)
    else:
        console.print(
            f"[green]✓[/green] repaired {safe_markup(id)} event export "
            f"({result['events']} events, {result['terminal_count']} terminal)"
        )


@project_app.command("list")
def project_list(json_out: bool = typer.Option(False, "--json")) -> None:
    projects = _app().projects.list()
    if json_out:
        emit_json([project.model_dump(mode="json") for project in projects])
        return
    table = Table("ID", "State", "Root")
    for project in projects:
        table.add_row(safe_line(project.id), safe_line(project.state), safe_line(project.root))
    console.print(table)


@project_app.command("relocate")
def project_relocate(
    id: str = typer.Option(..., "--id"), root: Path = typer.Option(..., "--root")
) -> None:
    try:
        project = _app().projects.relocate(id, root)
    except ProjectError as exc:
        _fail(str(exc))
    console.print(
        f"[green]✓[/green] relocated project {safe_markup(project.id)} to "
        f"{safe_markup(project.root)}"
    )


@app.command("mcp")
def mcp(action: str = typer.Argument("serve")) -> None:
    """MCP server (planned for v0.3)."""
    _fail("`openagent mcp serve` arrives in v0.3; not available in v0.1.")


# --------------------------------------------------------------------------- provider


@provider_app.command("add")
def provider_add(
    name: str = typer.Argument(..., help="Connection name, e.g. deepseek-main."),
    type: str = typer.Option(..., "--type", help=f"Provider type: {', '.join(preset_names())}"),
    protocol: str | None = typer.Option(
        None, "--protocol", help="openai-chat|openai-responses|anthropic-messages"
    ),
    base_url: str | None = typer.Option(None, "--base-url"),
    region: str | None = typer.Option(None, "--region"),
    workspace_id: str | None = typer.Option(None, "--workspace-id"),
    key_env: str | None = typer.Option(
        None, "--key-env", help="Reference an env var instead of storing a key."
    ),
    no_key: bool = typer.Option(
        False, "--no-key", help="Local provider needs no key (e.g. ollama)."
    ),
) -> None:
    """Register an API provider. The key is prompted with hidden input (never passed as an argument)."""
    oa = _app()
    if oa.providers.get(name):
        _fail(f"provider {name!r} already exists")
    credential_source = "none" if no_key else ("env" if key_env else "keychain")
    api_key = None
    if credential_source == "keychain":
        # Hidden prompt only — a key is NEVER accepted as a command argument (spec §30, §9), so it
        # cannot land in shell history, `ps` output, or CI logs.
        preset = get_preset(type)
        label = preset.credential_label if preset and preset.credential_label else "API key"
        if preset and preset.credential_hint:
            console.print(f"[dim]{safe_markup(preset.credential_hint)}[/dim]")
        api_key = typer.prompt(f"{label} for {name}", hide_input=True)
    proto = Protocol(protocol) if protocol else None
    try:
        provider = oa.providers.add(
            name=name,
            provider_type=type,
            protocol=proto,
            base_url=base_url,
            api_key=api_key,
            key_env=key_env,
            credential_source=credential_source,
            region=region,
            workspace_id=workspace_id,
        )
    except ProviderValidationError as exc:
        _fail(str(exc))
    console.print(
        f"[green]✓[/green] provider [bold]{safe_markup(name)}[/bold] added "
        f"({safe_markup(provider.provider_type)}, {safe_markup(provider.protocol.value)})"
    )
    console.print(f"  test it: [bold]openagent provider test {safe_markup(name)}[/bold]")


@provider_app.command("list")
def provider_list(json_out: bool = typer.Option(False, "--json")) -> None:
    oa = _app()
    providers = oa.providers.list()
    if json_out:
        emit_json([p.model_dump(mode="json") for p in providers])
        return
    table = Table("Name", "Type", "Protocol", "Base URL", "Key")
    for p in providers:
        cred = p.credential.type if isinstance(p.credential.type, str) else p.credential.type.value
        table.add_row(
            safe_line(p.name),
            safe_line(p.provider_type),
            safe_line(p.protocol.value),
            safe_line(p.base_url or "(preset)"),
            safe_line(cred),
        )
    console.print(table)


@provider_app.command("test")
def provider_test(
    name: str = typer.Argument(...),
    model: str | None = typer.Option(
        None, "--model", help="Also validate this model with a real capability probe."
    ),
) -> None:
    """Check a provider connection (spec §18).

    Without ``--model`` this only proves the **catalog is reachable** — it is deliberately NOT
    reported as "authenticated" or "API key valid", because a catalog can be public and reaching it
    proves nothing about the key or about any model's compatibility. Pass ``--model`` to run a real
    probe.
    """
    oa = _app()
    if model:
        _print_probe(name, model, json_out=False, refresh=True)
        return
    result = _run(oa.providers.test(name))
    if not result.ok:
        _fail(f"{name}: {result.detail}")
    console.print(
        f"[green]✓[/green] {safe_markup(name)}: catalog reachable "
        f"([dim]{safe_markup(result.detail)}[/dim])"
    )
    console.print("[yellow]The API key and model inference have not yet been validated.[/yellow]")
    console.print(
        f"  Validate them: [bold]openagent provider probe {safe_markup(name)} "
        "--model <publisher/model>[/bold]"
    )


@provider_app.command("models")
def provider_models(
    name: str = typer.Argument(...),
    search: str | None = typer.Option(
        None, "--search", help="Filter by model id (local, no network)."
    ),
    owner: str | None = typer.Option(None, "--owner", help="Filter by publisher (owned_by)."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List a provider's catalog models (spec §17.3).

    A catalog entry is **not** a capability claim: mixed catalogs (NVIDIA Build) return chat,
    embedding, rerank and vision models alike, so ``capabilities`` is always ``null`` here. Use
    ``openagent provider probe`` to find out what a model can actually do.
    """
    oa = _app()
    models = filter_models(_run(oa.providers.remote_models(name)), search=search, owner=owner)
    if json_out:
        emit_json(
            {
                "provider": name,
                "models": [
                    {"id": m.id, "owned_by": m.owned_by, "capabilities": None} for m in models
                ],
            }
        )
        return
    if not models:
        console.print(
            "[yellow]no models returned (provider may lack a /models endpoint, or the "
            "filters matched nothing)[/yellow]"
        )
        return
    preset = get_preset(_provider_type(oa, name))
    if preset is not None and preset.catalog_is_mixed:
        console.print(
            "[yellow]This catalog contains chat, embedding, reranking, vision and other model "
            "types.\nA catalog entry is not automatically compatible with OpenAgent agents — "
            "validate the model before creating an agent.[/yellow]\n"
        )
    table = Table("Model", "Publisher", "Note")
    for m in models:
        note = "may not be a chat model" if looks_non_chat(m.id) else ""
        table.add_row(safe_line(m.id), safe_line(m.owned_by or "—"), note)
    console.print(table)
    console.print(
        f"[dim]{len(models)} model(s). Capabilities are unknown until probed:[/dim] "
        f"[bold]openagent provider probe {safe_markup(name)} --model <id>[/bold]"
    )


@provider_app.command("probe")
def provider_probe(
    name: str = typer.Argument(...),
    model: str = typer.Option(..., "--model", help="Model id, e.g. publisher/model."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
    refresh: bool = typer.Option(False, "--refresh", help="Ignore any cached probe result."),
) -> None:
    """Really validate a model: text, streaming, and tool calling (spec §17.4).

    This is the only thing that may be called validation. The API key is never printed, and a
    capability is reported only when it was actually observed. Exits non-zero when the model is not
    verified agent-compatible.
    """
    _print_probe(name, model, json_out=json_out, refresh=refresh)


def _print_probe(name: str, model: str, *, json_out: bool, refresh: bool) -> None:
    oa = _app()
    try:
        probe = _run(oa.providers.probe_model(name, model, refresh=refresh))
    except ProviderValidationError as exc:
        _fail(str(exc))
    if json_out:
        emit_json({"provider": name, **probe.to_dict()})
    else:
        caps = probe.capabilities
        mark = {
            True: "[green]yes[/green]",
            False: "[red]no[/red]",
            None: "[yellow]unverified[/yellow]",
        }
        console.print(f"[bold]{safe_markup(name)}[/bold] · {safe_markup(model)}")
        # `mark` already distinguishes unverified; bool() collapsed None into "no", making that
        # branch unreachable and reporting an untested model as known-unsupported (spec §20).
        console.print(f"  text:         {mark[caps.text]}")
        console.print(f"  streaming:    {mark[caps.streaming]}")
        console.print(f"  tool calling: {mark[caps.tool_calling]}")
        colour = "green" if probe.agent_compatible else "yellow"
        console.print(f"  [{colour}]{safe_markup(probe.message())}[/{colour}]")
        remedy = _probe_remedy(oa, name, probe.category)
        if remedy:
            console.print(f"  [dim]{safe_markup(remedy)}[/dim]")
        if probe.detail:
            console.print(f"  [dim]{safe_markup(probe.detail, 300)}[/dim]")
    if not probe.agent_compatible:
        raise typer.Exit(1)


def _provider_type(oa: OpenAgentApp, name: str) -> str:
    provider = oa.providers.get(name)
    return provider.provider_type if provider else ""


def _probe_remedy(oa: OpenAgentApp, name: str, category: str) -> str:
    """Provider-specific next step for a failed probe (spec §18).

    The probe's own verdict is provider-neutral (it is shared by every adapter); this adds the one
    concrete action the user can take, using the preset's published URLs rather than a hardcoded
    vendor string. Never includes the key or the request body.
    """

    preset = get_preset(_provider_type(oa, name))
    if preset is None:
        return ""
    if category == PROBE_UNAUTHORIZED and preset.catalog_url:
        return f"Generate or replace the key at {preset.catalog_url}"
    if category == PROBE_NOT_FOUND:
        return f"Refresh the catalog: openagent provider models {name}"
    if category == PROBE_RATE_LIMITED:
        return "Wait and retry, or check your quota with the provider."
    return ""


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
        table.add_row(
            preset.provider_type,
            preset.label,
            preset.protocol.value,
            "yes" if preset.needs_key else "no",
        )
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
    allow_unverified_model: bool = typer.Option(
        False,
        "--allow-unverified-model",
        help="Create the agent even though its model has no verified capability probe.",
    ),
    model_override_reason: str | None = typer.Option(None, "--model-override-reason"),
    reasoning_effort: str | None = typer.Option(None, "--reasoning-effort"),
) -> None:
    """Add an agent (same as top-level `add`)."""
    add_agent(
        name=name,
        title=title,
        description=description,
        provider=provider,
        model=model,
        cli=cli,
        tag=tag,
        system_prompt=system_prompt,
        profile=profile,
        allow_unverified_model=allow_unverified_model,
        model_override_reason=model_override_reason,
        reasoning_effort=reasoning_effort,
    )


@agent_app.command("list")
def agent_list(json_out: bool = typer.Option(False, "--json")) -> None:
    _print_agents(_app(), json_out)


@agent_app.command("show")
def agent_show(name: str = typer.Argument(...)) -> None:
    agent = _app().agents.get(name)
    if not agent:
        _fail(f"agent {name!r} not found")
    emit_json(agent.model_dump(mode="json"))


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
        emit_json([a.model_dump(mode="json") for a in agents])
        return
    table = Table("Name", "Title", "Runtime", "Tags", "Profile")
    for a in agents:
        rt = a.runtime
        rtype = rt.type if isinstance(rt.type, str) else rt.type.value
        runtime = f"{rt.cli}-cli" if rtype == "cli" else f"api:{rt.provider}"
        table.add_row(
            safe_line(a.name),
            safe_line(a.title or "—"),
            safe_line(runtime),
            safe_line(", ".join(a.tags) or "—"),
            safe_line(a.permission_profile),
        )
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
