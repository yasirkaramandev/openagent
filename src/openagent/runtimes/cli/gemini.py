"""Gemini CLI adapter (spec §11).

Runs ``gemini -p`` headlessly and maps its result onto :class:`NormalizedEvent`s. Three decisions
here are deliberately more conservative than the documentation would allow, and each one is a claim
this adapter refuses to make on the user's behalf.

**JSON output is probed, never assumed.** ``--output-format json`` is documented, and there are
released versions where passing it produces ``Unknown arguments: output-format`` and the help text
(google-gemini/gemini-cli#9009). An adapter that trusts the docs turns that into a run which fails
for reasons the user cannot connect to anything they did. So support is established by asking the
installed binary, and a CLI that does not have it is still usable — with ``structured_events``
reported as ``False``, which is the honest answer rather than a broken one.

**The result is one object at the end, so live streaming is not claimed.** Headless ``gemini``
returns ``{response, stats, error}`` once the run finishes. OpenAgent could emit a synthetic
``message.delta`` per line to make the run console look busy, and that is exactly what spec §11.3
forbids: a fabricated delta is indistinguishable from a real one downstream, so anything reasoning
about latency or partial output would be reasoning about a fiction. ``structured_events`` and
``live_structured_events`` are therefore separate answers, and the second is ``False``.

**Resume is UNSUPPORTED until a live spike says otherwise.** ``/chat save`` and ``/chat resume``
are interactive checkpoint commands; no headless session-id resume contract appears in the
documentation this was written against. Re-sending the prompt history is *not* native resume and
must not be labelled as it — the CLI would start a new session with a longer prompt, which behaves
differently and costs differently. :data:`RESUME_SPIKE_REQUIRED` records what would have to be
observed to change the answer.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...core.errors import ErrorType, classify_http_status
from ...core.events import EventType, NormalizedEvent
from ...core.models import CliInstallation
from ...core.permissions import READ_ONLY, SAFE_EDIT
from ...security.process import (
    ManagedProcess,
    TerminationOutcome,
    TerminationResult,
    minimal_environment,
)
from .base import (
    AuthStatus,
    CliCapabilities,
    CliModelDiscoveryContext,
    CliRunRequest,
    run_buffered_cli,
)
from .installations import inspect_installation
from .locator import CliLocation
from .locator import locate_candidates as locate_cli_candidates
from .model_discovery import CliModelDiscoveryResult, CliModelOption
from .updates import check_update as inspect_update
from .updates import perform_update as execute_update

SOURCE = "gemini-cli"

#: Documented ``--approval-mode`` values.
APPROVAL_DEFAULT = "default"
APPROVAL_AUTO_EDIT = "auto_edit"
APPROVAL_YOLO = "yolo"

#: What a live spike must observe before ``resumable`` may become ``True`` (spec §11.5).
RESUME_SPIKE_REQUIRED = (
    "a headless flag that resumes a named session and a machine-readable session id in the "
    "run's own output; replaying prompt history is not native resume"
)

#: Authentication variables the Gemini CLI documents. Names only — values never leave the child
#: environment, and OpenAgent stores the *state* and the *source*, never the credential (spec §11.2).
AUTH_ENVIRONMENT_VARIABLES = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_LOCATION",
)


@dataclass(frozen=True)
class GeminiPermissionMapping:
    """How one OpenAgent permission profile becomes Gemini CLI flags (spec §11.4)."""

    approval_mode: str
    sandbox: bool
    #: Tools to withhold. Empty means "whatever the CLI allows by default".
    denied_tools: tuple[str, ...] = ()
    #: The allowlist that accompanies a denylist. Empty means no allowlist is imposed.
    allowed_tools: tuple[str, ...] = ()
    #: Prepended to the prompt for profiles that are a *posture* rather than a flag.
    prompt_prefix: str = ""
    #: Why this mapping is what it is, for the wizard and Doctor to show.
    note: str = ""


#: Write and shell tools, withheld for the read-only and plan postures. Named explicitly because
#: "read-only" has to mean something enforceable, not just an approval prompt the model can talk
#: its way past.
MUTATING_TOOLS = ("run_shell_command", "write_file", "replace")

#: The tools a read-only posture *may* use. Sent as an allowlist (``tools.core``) alongside the
#: denylist, because a denylist alone admits by default any tool a future CLI release adds — and the
#: whole point of this profile is that what it can do is bounded now, not bounded as of today's
#: release.
READ_ONLY_TOOLS = (
    "read_file",
    "read_many_files",
    "list_directory",
    "glob",
    "grep_search",
)

#: Redirects the CLI's *system* settings file. System settings sit above user and project settings
#: in the documented hierarchy, which is the only layer a workspace's own ``.gemini/settings.json``
#: cannot widen — and the workspace is the attacker-influenced input here.
SYSTEM_SETTINGS_ENV = "GEMINI_CLI_SYSTEM_SETTINGS_PATH"

#: Backwards-compatible alias for the internal name this constant used to have.
_MUTATING_TOOLS = MUTATING_TOOLS

_PERMISSION_MAPPINGS: dict[str, GeminiPermissionMapping] = {
    READ_ONLY: GeminiPermissionMapping(
        approval_mode=APPROVAL_DEFAULT,
        sandbox=True,
        denied_tools=MUTATING_TOOLS,
        allowed_tools=READ_ONLY_TOOLS,
        note="sandboxed; write and shell tools withheld by system-settings override",
    ),
    SAFE_EDIT: GeminiPermissionMapping(
        approval_mode=APPROVAL_AUTO_EDIT,
        sandbox=True,
        note="sandboxed; edits auto-approved, shell still prompts",
    ),
    "full": GeminiPermissionMapping(
        approval_mode=APPROVAL_DEFAULT,
        sandbox=True,
        note="sandboxed; every tool call goes through the CLI's own approval",
    ),
    "plan": GeminiPermissionMapping(
        approval_mode=APPROVAL_DEFAULT,
        sandbox=True,
        denied_tools=MUTATING_TOOLS,
        allowed_tools=READ_ONLY_TOOLS,
        prompt_prefix=(
            "Produce a plan only. Do not modify any file and do not run any command.\n\n"
        ),
        note="read-only tools plus a planning instruction",
    ),
}


def permission_mapping(profile_name: str) -> GeminiPermissionMapping:
    """Map an OpenAgent profile onto Gemini CLI flags.

    ``yolo`` is deliberately absent. The CLI offers ``--yolo`` / ``--approval-mode yolo``, which
    auto-approves every action including shell commands; OpenAgent does not surface it as a normal
    choice, because a profile that a user can pick from a list is a profile they will pick without
    reading what it does. An unknown profile falls back to the most restrictive mapping rather than
    the most permissive — the direction a default should fail in.
    """

    return _PERMISSION_MAPPINGS.get(profile_name, _PERMISSION_MAPPINGS[READ_ONLY])


def system_settings_document(mapping: GeminiPermissionMapping) -> dict[str, Any]:
    """The settings OpenAgent imposes on one run, as the CLI's own documented schema.

    Only the keys the profile actually constrains are emitted. A profile that is *meant* to edit
    must not be handed a read-only allowlist just because the override mechanism exists.
    """

    document: dict[str, Any] = {
        # The prompt carries the user's source. It does not go to a vendor telemetry endpoint
        # because OpenAgent happened to launch the CLI.
        "telemetry": {"enabled": False, "logPrompts": False},
    }
    if mapping.denied_tools:
        document["tools"] = {
            "core": list(mapping.allowed_tools),
            "exclude": list(mapping.denied_tools),
        }
    return document


@contextmanager
def system_settings_file(mapping: GeminiPermissionMapping) -> Iterator[Path | None]:
    """Materialize the policy as a private file for the lifetime of one run.

    Outside the workspace, so a run cannot rewrite the policy that governs it through a relative
    path; ``0700`` directory and ``0600`` file, so another local user cannot read or edit it between
    write and exec; removed afterwards, because a stale policy file silently governing a later run
    is its own bug.

    Yields ``None`` when the profile constrains nothing, so callers do not set the environment
    variable — pointing the CLI at an empty system-settings file is a real change in behaviour
    (it overrides whatever the user's actual system settings say).
    """

    document = system_settings_document(mapping)
    if not document.get("tools"):
        yield None
        return

    directory = Path(tempfile.mkdtemp(prefix="openagent-gemini-policy-"))
    try:
        os.chmod(directory, 0o700)
        path = directory / "settings.json"
        # Created 0600 from the start rather than chmod'ed after: between an 0644 create and the
        # chmod there is a window in which another local user can read or replace it.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        yield path
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def apply_system_settings(env: dict[str, str], path: Path | None) -> dict[str, str]:
    """Point the CLI at the override, or leave the environment alone when there is none."""

    if path is not None:
        env[SYSTEM_SETTINGS_ENV] = str(path)
    return env


def build_command(
    request: CliRunRequest,
    *,
    executable: str = "gemini",
    json_output: bool,
) -> list[str]:
    """The argv for one headless run.

    ``json_output`` comes from :func:`probe_json_output_support`, not from a version guess: the
    flag exists in some releases and not others, and passing it where it does not exist fails the
    run with an argument error.
    """

    mapping = permission_mapping(request.permission_profile)
    argv = [executable, "--prompt", mapping.prompt_prefix + request.prompt]

    if json_output:
        argv += ["--output-format", "json"]
    if request.model:
        argv += ["--model", request.model]
    argv += ["--approval-mode", mapping.approval_mode]
    if mapping.sandbox:
        argv.append("--sandbox")
    return argv


@dataclass(frozen=True)
class JsonOutputSupport:
    """Whether the installed binary accepts ``--output-format json`` (spec §11.2)."""

    supported: bool
    detail: str = ""

    @property
    def structured_events(self) -> bool:
        return self.supported


async def probe_json_output_support(
    executable: str, *, runner: Any | None = None
) -> JsonOutputSupport:
    """Ask the installed CLI whether it knows the flag, by reading its own help.

    Reading ``--help`` rather than running a real prompt: a probe that costs a model call is a
    probe that gets skipped, and skipping it puts the guess back.
    """

    run = runner or _run_help
    try:
        text = await run(executable)
    except Exception as exc:  # noqa: BLE001 - any probe failure means "unknown", never "yes"
        return JsonOutputSupport(False, f"could not read gemini --help ({exc.__class__.__name__})")
    if "--output-format" in text:
        return JsonOutputSupport(True, "gemini --help advertises --output-format")
    return JsonOutputSupport(
        False,
        "this gemini build does not advertise --output-format; run output will be plain text",
    )


async def _run_help(executable: str) -> str:
    process = await asyncio.create_subprocess_exec(
        executable,
        "--help",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
    except (TimeoutError, asyncio.TimeoutError):
        process.kill()
        await process.wait()
        raise
    return (stdout or b"").decode("utf-8", "replace") + (stderr or b"").decode("utf-8", "replace")


def map_result(payload: dict[str, Any], run_id: str) -> list[NormalizedEvent]:
    """Map the single terminal JSON object onto normalized events (spec §11.3).

    Emits at most one message and exactly one terminal event. No ``message.delta`` is synthesized:
    the text did not arrive incrementally, and a fabricated delta is indistinguishable downstream
    from one that did.
    """

    events: list[NormalizedEvent] = []
    error = payload.get("error")

    if isinstance(error, dict) and (error.get("message") or error.get("type")):
        return [
            NormalizedEvent(
                type=EventType.RUN_FAILED,
                run_id=run_id,
                source=SOURCE,
                data={
                    "error_type": _map_error_type(error).value,
                    "message": str(error.get("message") or error.get("type") or "gemini failed"),
                },
            )
        ]

    response = payload.get("response")
    if isinstance(response, str) and response:
        events.append(
            NormalizedEvent(
                type=EventType.MESSAGE_COMPLETED,
                run_id=run_id,
                source=SOURCE,
                data={"text": response},
            )
        )

    usage = _map_stats(payload.get("stats"))
    if usage:
        events.append(
            NormalizedEvent(type=EventType.USAGE_UPDATED, run_id=run_id, source=SOURCE, data=usage)
        )

    events.append(
        NormalizedEvent(type=EventType.RUN_COMPLETED, run_id=run_id, source=SOURCE, data={})
    )
    return events


def _map_error_type(error: dict[str, Any]) -> ErrorType:
    code = error.get("code")
    if isinstance(code, int) and code:
        return classify_http_status(code)
    text = str(error.get("type") or error.get("message") or "").lower()
    if "auth" in text:
        return ErrorType.AUTHENTICATION_FAILED
    if "quota" in text or "rate" in text:
        return ErrorType.PROVIDER_RATE_LIMITED
    return ErrorType.COMMAND_FAILED


def _map_stats(stats: object) -> dict[str, Any]:
    """Pull token counters out of ``stats.models`` without asserting a shape it may not have.

    The documented structure is ``{models, tools, files}`` with per-model entries; the exact
    counter names are not pinned here, so anything integer-valued and recognisably a token count is
    summed and anything else is left alone rather than coerced.
    """

    if not isinstance(stats, dict):
        return {}
    models = stats.get("models")
    if not isinstance(models, dict):
        return {}
    totals: dict[str, int] = {}
    for entry in models.values():
        if not isinstance(entry, dict):
            continue
        tokens = entry.get("tokens")
        if not isinstance(tokens, dict):
            continue
        for key, value in tokens.items():
            if isinstance(value, int) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
    return {"tokens": totals} if totals else {}


class GeminiCliAdapter:
    """Gemini CLI, run headlessly (spec §11).

    Marked experimental: the event mapping is fixture-validated and the resume contract is unproven.
    """

    cli_type = "gemini"

    def __init__(self, executable: str | None = None) -> None:
        self._explicit_executable = executable
        self.location: CliLocation = locate_cli_candidates("gemini", explicit_path=executable)
        self.executable = executable or self.location.active_executable
        self._json_output: JsonOutputSupport | None = None
        #: Live runs, by run id, so cancel() signals the right process tree.
        self._processes: dict[str, ManagedProcess] = {}
        #: The last model-discovery attempt, for the wizard to show method and reason.
        self.last_model_discovery: CliModelDiscoveryResult | None = None

    # ------------------------------------------------------------------ discovery

    async def detect(self) -> CliInstallation | None:
        self.location = await asyncio.to_thread(self.locate_candidates)
        install = inspect_installation(
            "gemini", self.location, adapter="gemini-headless-json", experimental=True
        )
        if install is not None:
            self.executable = install.executable
        return install

    def locate_candidates(self) -> CliLocation:
        self.location = locate_cli_candidates("gemini", explicit_path=self._explicit_executable)
        return self.location

    async def inspect_installation(self) -> CliInstallation | None:
        return await self.detect()

    async def json_output_support(self, *, refresh: bool = False) -> JsonOutputSupport:
        if self._json_output is None or refresh:
            if not self.executable:
                self._json_output = JsonOutputSupport(False, "gemini is not installed")
            else:
                self._json_output = await probe_json_output_support(self.executable)
        return self._json_output

    # ------------------------------------------------------------------ capabilities

    async def capabilities(self) -> CliCapabilities:
        support = await self.json_output_support()
        return CliCapabilities(
            structured_events=support.structured_events,
            # Not "we have not implemented it" — the contract does not exist in the documentation
            # this was written against, and claiming it would make the wizard offer a resume that
            # silently starts a new conversation (spec §11.5).
            resumable=False,
            edits_files=True,
            runs_commands=True,
            experimental=True,
        )

    @property
    def live_structured_events(self) -> bool:
        """Headless ``gemini`` returns one object at the end; nothing streams (spec §11.3)."""

        return False

    async def inspect_auth(self) -> AuthStatus:
        """Report *which* credential mechanism is configured, never the credential.

        Three documented paths — Google login, a Gemini API key, and Vertex AI — and OpenAgent
        stores only which one is in play. Copying the value into OpenAgent's own store would create
        a second copy of a secret that the CLI already manages, with no way to keep the two in sync
        (spec §11.2).
        """

        present = [name for name in AUTH_ENVIRONMENT_VARIABLES if os.environ.get(name)]
        if "GEMINI_API_KEY" in present or "GOOGLE_API_KEY" in present:
            return AuthStatus(
                authenticated=True,
                detail="API key present in the environment",
                environment_names=present,
                source="environment",
            )
        if "GOOGLE_APPLICATION_CREDENTIALS" in present or "GOOGLE_CLOUD_PROJECT" in present:
            return AuthStatus(
                authenticated=True,
                detail="Vertex AI / Application Default Credentials configured",
                environment_names=present,
                source="environment",
            )
        # An interactive Google login leaves no environment variable, so absence is not proof of
        # anything. Non-blocking: the CLI's own error is more useful than a guess about why it
        # might fail.
        return AuthStatus(
            authenticated=False,
            detail="no credential variable set; an interactive Google login cannot be detected here",
            blocking=False,
            environment_names=[],
            source="none",
        )

    # ------------------------------------------------------------------ model discovery

    #: How the wizard labels where this list came from.
    model_discovery_method = "gemini-config-allowlist"

    async def list_models(self, context: CliModelDiscoveryContext | None = None) -> list[str]:
        """Enumerate models this installation can actually reach — never a documented guess.

        The order is the one spec §11.4 requires, and each step is skipped rather than faked when it
        is unavailable:

        1. the project's / user's own settings, where a policy allowlist means an administrator has
           already answered the question with authority over the machine;
        2. the credential's API catalog, when a Gemini key is present in the environment the CLI
           would actually run under;
        3. nothing — the wizard then offers a manual id and "use the CLI's own default".

        There is deliberately no hardcoded alias list, and no listing command is invented: the Gemini
        CLI documents none, and a stale alias produces a run that fails for a reason the user cannot
        connect to anything they did.
        """

        context = context or CliModelDiscoveryContext()

        allowlist = _settings_allowlist(context.project_root)
        if allowlist:
            self.last_model_discovery = CliModelDiscoveryResult(
                cli_type=self.cli_type,
                available=True,
                method="gemini-settings-allowlist",
                options=[
                    CliModelOption(id=model, display_name=model, source="gemini settings policy")
                    for model in allowlist
                ],
            )
            return allowlist

        catalog = await self._api_catalog(context)
        if catalog is not None:
            self.last_model_discovery = CliModelDiscoveryResult(
                cli_type=self.cli_type,
                available=True,
                method="gemini-api-catalog",
                options=[
                    CliModelOption(id=model, display_name=model, source="Gemini API catalog")
                    for model in catalog
                ],
            )
            return catalog

        self.last_model_discovery = CliModelDiscoveryResult(
            cli_type=self.cli_type,
            available=False,
            method=self.model_discovery_method,
            error=(
                "the Gemini CLI documents no model listing command, no settings allowlist was found, "
                "and no Gemini API key is present to read the catalog with; type a model id, or "
                "leave it blank to use the CLI's own default"
            ),
        )
        return []

    async def _api_catalog(self, context: CliModelDiscoveryContext) -> list[str] | None:
        """Read the credential's own catalog, when a credential is actually present.

        Scoped to the environment the CLI would run under, so the answer reflects the key that will
        serve the run rather than whatever happens to be exported in the parent shell.
        """

        environment = context.environment or dict(os.environ)
        api_key = environment.get("GEMINI_API_KEY") or environment.get("GOOGLE_API_KEY")
        if not api_key:
            return None
        from ...providers.gemini_interactions import GeminiInteractionsAdapter

        adapter = GeminiInteractionsAdapter(api_key=api_key)
        try:
            models = await adapter.list_models()
        except Exception:  # noqa: BLE001 - a failed catalog read is "unknown", never a fabricated list
            return None
        finally:
            await adapter.transport.aclose()
        ids = [model.id for model in models if _serves_content(model)]
        return sorted(ids) or None

    # ------------------------------------------------------------------ runs

    def start_run(self, request: CliRunRequest) -> AsyncIterator[NormalizedEvent]:
        return self._drive(request)

    async def _drive(self, request: CliRunRequest) -> AsyncIterator[NormalizedEvent]:
        if not self.executable:
            yield NormalizedEvent(
                run_id=request.run_id,
                type=EventType.RUN_FAILED,
                source=SOURCE,
                data={
                    "error_type": ErrorType.CLI_NOT_FOUND.value,
                    "message": "gemini is not installed",
                },
            )
            return

        support = await self.json_output_support()
        argv = build_command(request, executable=self.executable, json_output=support.supported)
        mapping = permission_mapping(request.permission_profile)

        # The credential the *run* needs, and nothing else. A Gemini run must not receive
        # ANTHROPIC_API_KEY or OPENAI_API_KEY merely because the user has them exported (spec §19.2).
        env = minimal_environment(request.credential_env)

        # Enforced through the CLI's *system* settings layer, not an approval prompt and not an
        # environment variable of our own invention. The previous implementation set
        # GEMINI_EXCLUDE_TOOLS, which the CLI does not document — an unrecognised variable is
        # ignored, so "read-only" was a label on a run that could still write files and spawn
        # shells. System settings are also the one layer the workspace's own .gemini/settings.json
        # cannot widen, and the workspace is attacker-influenced input (spec §12.3).
        with system_settings_file(mapping) as policy_path:
            apply_system_settings(env, policy_path)

            proc = ManagedProcess(argv, cwd=request.workspace, env=env)
            self._processes[request.run_id] = proc
            try:
                if support.supported:
                    async for event in run_buffered_cli(
                        proc=proc, run_id=request.run_id, source=SOURCE, parser=_parse_run_output
                    ):
                        yield event
                else:
                    # No structured output on this build. The text is still the answer; it is
                    # reported as one completed message, with structured_events already advertised
                    # as False.
                    async for event in run_buffered_cli(
                        proc=proc, run_id=request.run_id, source=SOURCE, parser=_parse_plain_output
                    ):
                        yield event
            finally:
                self._processes.pop(request.run_id, None)

    async def cancel(self, run_id: str) -> TerminationResult:
        """Terminate the run's own process tree.

        Identity-checked by :class:`ManagedProcess`: a pid recorded at start is verified against the
        process's create time and executable before anything is signalled, so a reused pid cannot
        cause an unrelated process to be killed (spec §11.3).
        """

        proc = self._processes.get(run_id)
        if proc is not None:
            return await proc.cancel()
        return TerminationResult(TerminationOutcome.ALREADY_GONE)

    # ------------------------------------------------------------------ updates

    async def check_update(self):
        """Report whether an update is available *and whether OpenAgent may perform it*.

        Delegates to the shared provenance-checked updater rather than reimplementing it. Provenance
        decides the second answer (spec §11.5): an npm-global install can be updated with npm, a
        Homebrew install needs brew, and a binary someone copied into ``~/bin`` has no mechanism
        OpenAgent can infer — guessing one is how a working install becomes a broken one.
        """

        installation = await self.detect()
        if installation is None:
            raise RuntimeError("gemini is not installed")
        return await asyncio.to_thread(inspect_update, installation)

    async def perform_update(self, *, dry_run: bool = False, active_run_ids: Sequence[str] = ()):
        installation = await self.detect()
        if installation is None:
            raise RuntimeError("gemini is not installed")
        status = await asyncio.to_thread(inspect_update, installation)
        return await asyncio.to_thread(
            execute_update,
            installation,
            status,
            dry_run=dry_run,
            active_run_ids=active_run_ids,
        )

    # ------------------------------------------------------------------ resume

    def resume_run(self, session_id: str, prompt: str, request: CliRunRequest):
        raise NotImplementedError(
            f"Gemini CLI has no verified headless resume contract. Required to change this: "
            f"{RESUME_SPIKE_REQUIRED}."
        )


# ------------------------------------------------------------------------------- run output


def _parse_run_output(text: str, run_id: str) -> list[NormalizedEvent]:
    """Parse the single JSON document headless ``gemini --output-format json`` prints.

    The document may be pretty-printed across many lines, so the whole of stdout is parsed at once. A
    body that is not JSON at all is reported as a failure rather than silently dropped: the run
    produced output nobody can read, which is a different thing from a run that produced nothing.
    """

    stripped = text.strip()
    if not stripped:
        return []
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        # Some builds print a banner before the JSON. Try the outermost object before giving up.
        payload = _first_json_object(stripped)
        if payload is None:
            return [
                NormalizedEvent(
                    type=EventType.RUN_FAILED,
                    run_id=run_id,
                    source=SOURCE,
                    data={
                        "error_type": ErrorType.MALFORMED_STREAM.value,
                        "message": "gemini did not produce readable JSON output",
                    },
                )
            ]
    if not isinstance(payload, dict):
        return [
            NormalizedEvent(
                type=EventType.RUN_FAILED,
                run_id=run_id,
                source=SOURCE,
                data={
                    "error_type": ErrorType.MALFORMED_STREAM.value,
                    "message": "gemini output was not a JSON object",
                },
            )
        ]
    return map_result(payload, run_id)


def _parse_plain_output(text: str, run_id: str) -> list[NormalizedEvent]:
    """A build without ``--output-format``: the text *is* the answer."""

    body = text.strip()
    events: list[NormalizedEvent] = []
    if body:
        events.append(
            NormalizedEvent(
                type=EventType.MESSAGE_COMPLETED,
                run_id=run_id,
                source=SOURCE,
                data={"text": body},
            )
        )
    events.append(
        NormalizedEvent(type=EventType.RUN_COMPLETED, run_id=run_id, source=SOURCE, data={})
    )
    return events


def _first_json_object(text: str) -> Any | None:
    """Extract the first balanced ``{...}`` from a body that has a banner in front of it."""

    start = text.find("{")
    if start < 0:
        return None
    decoder = json.JSONDecoder()
    try:
        payload, _ = decoder.raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return payload


# ------------------------------------------------------------------------------- settings


#: Where the Gemini CLI reads configuration from, project scope before user scope.
_SETTINGS_PATHS = (".gemini/settings.json",)
_USER_SETTINGS = Path.home() / ".gemini" / "settings.json"


def _settings_allowlist(project_root: Path | None) -> list[str]:
    """Models an administrator has already restricted this installation to.

    A policy allowlist is the strongest answer available offline: someone with authority over the
    machine has stated which models may be used, which beats both a documented alias list and a
    catalog the key can see but policy forbids.
    """

    candidates: list[Path] = []
    if project_root is not None:
        candidates.extend(project_root / name for name in _SETTINGS_PATHS)
    candidates.append(_USER_SETTINGS)

    for path in candidates:
        models = _read_allowlist(path)
        if models:
            return models
    return []


def _read_allowlist(path: Path) -> list[str]:
    try:
        if not path.is_file():
            return []
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    for key in ("availableModels", "allowedModels", "models"):
        value = data.get(key)
        if isinstance(value, list):
            models = [item for item in value if isinstance(item, str) and item.strip()]
            if models:
                return models
        if isinstance(value, dict):
            allowed = value.get("allowed") or value.get("available")
            if isinstance(allowed, list):
                models = [item for item in allowed if isinstance(item, str) and item.strip()]
                if models:
                    return models
    return []


def _serves_content(model: Any) -> bool:
    """Whether a catalog entry can serve a generation request at all.

    Gemini's catalog includes embedding models. Filtering on the *provider's own* statement of which
    methods a model serves is not a guess about capability — it is reading what Google said.
    """

    methods = getattr(model, "supported_generation_methods", None)
    if methods is None:
        extra = model.model_dump() if hasattr(model, "model_dump") else {}
        methods = extra.get("supported_generation_methods")
    if not isinstance(methods, list) or not methods:
        # Nothing stated: keep the model. Dropping it would hide a usable model on the strength of
        # missing metadata.
        return True
    return any("generatecontent" in str(method).lower() for method in methods)
