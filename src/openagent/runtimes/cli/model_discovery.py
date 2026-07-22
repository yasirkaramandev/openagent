"""Honest, documented model discovery for Codex, Claude Code, and Antigravity."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ... import __version__
from ...core.errors import redact_secrets
from ...security.process import (
    capture_process_identity,
    minimal_environment,
    terminate_process_tree,
)
from .locator import CommandRunner, run_bounded

MAX_SCHEMA_BYTES = 4 * 1024 * 1024
MAX_PROTOCOL_BYTES = 4 * 1024 * 1024
MAX_MODEL_BODY_BYTES = 4 * 1024 * 1024
DISCOVERY_TIMEOUT_SECONDS = 15


class CliModelOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    source: str
    advertised: bool = True
    entitlement_verified: bool = False
    reasoning_efforts: list[str] = Field(default_factory=list)
    hidden: bool = False
    kind: str = "model"
    description: str | None = None


class CliModelDiscoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cli_type: str
    available: bool
    options: list[CliModelOption] = Field(default_factory=list)
    method: str = ""
    partial: bool = False
    error: str | None = None

    @property
    def models(self) -> list[str]:
        return [option.id for option in self.options]


@dataclass
class CapabilityCacheEntry:
    """A cached capability probe result with an expiry (spec §13.1).

    The previous cache was a bare ``dict[..., bool]`` that stored ``False`` on *any* failure —
    timeout, a transient temp-directory error, a nonzero exit during a flaky app-server start — and
    never re-checked, so one hiccup disabled Codex model discovery for the whole process. An entry
    now expires: a positive result is trusted for a long time (a CLI version's schema does not change
    under it), and a negative-or-transient one only briefly, so the next call retries.
    """

    supported: bool | None
    error_type: str | None
    checked_at: float
    expires_at: float


#: A confirmed capability is stable for the life of an installed CLI; re-probing it is pure cost.
_CAPABILITY_POSITIVE_TTL = 3600.0
#: A negative or transient result is retried soon — the failure may have been a fluke.
_CAPABILITY_NEGATIVE_TTL = 30.0

_SCHEMA_CACHE: dict[tuple[str, str], CapabilityCacheEntry] = {}


def _probe_schema_supports_model_list(
    executable: str, runner: CommandRunner
) -> tuple[bool, str | None]:
    """Actually run the schema probe. Returns ``(supported, error_type)``; ``error_type`` is the
    reason a negative result should be treated as short-lived rather than durable."""

    with tempfile.TemporaryDirectory(prefix="openagent-codex-schema-") as temporary:
        output = Path(temporary)
        try:
            result = runner(
                [executable, "app-server", "generate-json-schema", "--out", str(output)],
                DISCOVERY_TIMEOUT_SECONDS,
                MAX_SCHEMA_BYTES,
            )
            if result.returncode != 0:
                return False, "nonzero_exit"
            total = 0
            for path in output.rglob("*.json"):
                if not path.is_file():
                    continue
                total += path.stat().st_size
                if total > MAX_SCHEMA_BYTES:
                    return False, "schema_too_large"
                if "model/list" in path.read_text(encoding="utf-8", errors="replace"):
                    return True, None
            return False, None
        except Exception as exc:
            return False, type(exc).__name__


def _schema_supports_model_list(
    executable: str,
    version: str | None,
    runner: CommandRunner = run_bounded,
    *,
    refresh: bool = False,
) -> bool:
    key = (str(Path(executable).resolve()), version or "unknown")
    now = time.monotonic()
    entry = _SCHEMA_CACHE.get(key)
    if entry is not None and not refresh and entry.supported is not None and now < entry.expires_at:
        return entry.supported
    supported, error_type = _probe_schema_supports_model_list(executable, runner)
    ttl = _CAPABILITY_POSITIVE_TTL if supported else _CAPABILITY_NEGATIVE_TTL
    _SCHEMA_CACHE[key] = CapabilityCacheEntry(supported, error_type, now, now + ttl)
    return supported


def _reasoning_efforts(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            value = entry
        elif isinstance(entry, dict):
            value = str(
                entry.get("reasoningEffort") or entry.get("effort") or entry.get("value") or ""
            )
        else:
            value = ""
        if value and value not in values:
            values.append(value)
    return values


def parse_codex_model_page(payload: object) -> tuple[list[CliModelOption], str | None]:
    """Parse one model/list result page while preserving server-provided ordering."""

    if not isinstance(payload, dict):
        raise ValueError("Codex model/list result is not an object")
    raw_models = payload.get("data", payload.get("models", []))
    if not isinstance(raw_models, list):
        raise ValueError("Codex model/list data is not a list")
    options: list[CliModelOption] = []
    for raw in raw_models:
        if not isinstance(raw, dict):
            raise ValueError("Codex model/list contains a malformed entry")
        model_id = raw.get("id") or raw.get("model")
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("Codex model entry omitted id")
        hidden = bool(raw.get("hidden", False))
        if hidden:
            continue
        display = raw.get("displayName") or raw.get("display_name") or model_id
        options.append(
            CliModelOption(
                id=model_id,
                display_name=str(display),
                source="codex-app-server",
                advertised=True,
                entitlement_verified=False,
                reasoning_efforts=_reasoning_efforts(raw.get("supportedReasoningEfforts")),
                hidden=hidden,
                description=str(raw.get("description")) if raw.get("description") else None,
            )
        )
    cursor = payload.get("nextCursor") or payload.get("next_cursor")
    return options, str(cursor) if cursor else None


async def _read_json_line(
    stream: asyncio.StreamReader, *, deadline: float, byte_state: dict[str, int]
) -> dict[str, Any]:
    remaining = max(0.01, deadline - asyncio.get_running_loop().time())
    raw = await asyncio.wait_for(stream.readline(), timeout=remaining)
    if not raw:
        raise RuntimeError("Codex app-server closed stdout before responding")
    byte_state["total"] += len(raw)
    if byte_state["total"] > MAX_PROTOCOL_BYTES:
        raise ValueError("Codex app-server output exceeded discovery limit")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Codex app-server emitted malformed JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Codex app-server emitted a non-object message")
    return value


#: How much of a chatty app-server's stderr to keep for diagnostics. Small: it is a tail for an
#: error message, not a log.
_STDERR_TAIL_LIMIT = 8 * 1024


async def _drain_stderr(
    stream: asyncio.StreamReader, tail_out: bytearray, *, on_overflow: Callable[[], None]
) -> None:
    """Continuously read ``stream`` to EOF, retaining only the last ``_STDERR_TAIL_LIMIT`` bytes.

    A single ``read(n)`` returns the *first* chunk and then stops draining, so a Codex app-server
    that keeps writing to stderr fills the OS pipe buffer, blocks on its next write, and stops
    answering on stdout — deadlocking the handshake (which then only fails on the deadline). Reading
    in a loop keeps the pipe empty. Past ``MAX_PROTOCOL_BYTES`` total the process is a runaway:
    ``on_overflow`` terminates it and draining stops (spec §13.2).
    """

    total = 0
    while True:
        try:
            chunk = await stream.read(65536)
        except Exception:
            return
        if not chunk:
            return
        total += len(chunk)
        tail_out.extend(chunk)
        if len(tail_out) > _STDERR_TAIL_LIMIT:
            del tail_out[:-_STDERR_TAIL_LIMIT]
        if total > MAX_PROTOCOL_BYTES:
            on_overflow()
            return


async def discover_codex_models(
    executable: str,
    *,
    version: str | None = None,
    schema_runner: CommandRunner = run_bounded,
    refresh: bool = False,
) -> CliModelDiscoveryResult:
    """Handshake with the installed Codex app-server and page ``model/list`` without a turn.

    ``refresh`` bypasses the capability cache so a transient failure can be re-checked on demand.
    """

    schema_ok = await asyncio.to_thread(
        _schema_supports_model_list, executable, version, schema_runner, refresh=refresh
    )
    if not schema_ok:
        return CliModelDiscoveryResult(
            cli_type="codex",
            available=False,
            method="codex-app-server",
            error="installed Codex schema does not advertise model/list; use CLI default or manual id",
        )
    proc: asyncio.subprocess.Process | None = None
    identity = None
    stderr_task: asyncio.Task[None] | None = None
    stderr_tail = bytearray()
    try:
        proc = await asyncio.create_subprocess_exec(
            executable,
            "app-server",
            "--listen",
            "stdio://",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=minimal_environment(),
        )
        identity = capture_process_identity(proc.pid)
        if proc.stdin is None or proc.stdout is None or proc.stderr is None:
            raise RuntimeError("Codex app-server stdio pipes unavailable")

        def _kill_runaway() -> None:
            with contextlib.suppress(Exception):
                if proc is not None:
                    proc.kill()

        stderr_task = asyncio.create_task(
            _drain_stderr(proc.stderr, stderr_tail, on_overflow=_kill_runaway)
        )

        async def send(message: dict[str, Any]) -> None:
            assert proc is not None and proc.stdin is not None
            encoded = json.dumps(message, separators=(",", ":")).encode() + b"\n"
            proc.stdin.write(encoded)
            await proc.stdin.drain()

        await send(
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "openagent",
                        "title": "OpenAgent",
                        "version": __version__,
                    },
                    "capabilities": {"optOutNotificationMethods": []},
                },
            }
        )
        deadline = asyncio.get_running_loop().time() + DISCOVERY_TIMEOUT_SECONDS
        byte_state = {"total": 0}
        while True:
            message = await _read_json_line(proc.stdout, deadline=deadline, byte_state=byte_state)
            if message.get("id") != 1:
                continue
            if message.get("error"):
                raise RuntimeError("Codex app-server initialize was rejected")
            break
        await send({"method": "initialized", "params": {}})

        request_id = 2
        cursor: str | None = None
        options: list[CliModelOption] = []
        seen_ids: set[str] = set()
        while True:
            params: dict[str, Any] = {"includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            await send({"method": "model/list", "id": request_id, "params": params})
            while True:
                message = await _read_json_line(
                    proc.stdout, deadline=deadline, byte_state=byte_state
                )
                if message.get("id") != request_id:
                    continue  # notification or unrelated response
                if message.get("error"):
                    raise RuntimeError("Codex app-server model/list was rejected")
                page, cursor = parse_codex_model_page(message.get("result"))
                for option in page:
                    if option.id not in seen_ids:
                        seen_ids.add(option.id)
                        options.append(option)
                break
            if not cursor:
                break
            request_id += 1
            if request_id > 100:
                raise ValueError("Codex model/list pagination exceeded 98 pages")
        return CliModelDiscoveryResult(
            cli_type="codex",
            available=True,
            options=options,
            method="codex-app-server model/list",
        )
    except Exception as exc:
        detail = str(exc)[:400]
        tail = redact_secrets(bytes(stderr_tail).decode("utf-8", "replace")).strip()
        if tail:
            detail = f"{detail}; app-server stderr (tail): {tail[-200:]}"
        return CliModelDiscoveryResult(
            cli_type="codex",
            available=False,
            method="codex-app-server model/list",
            error=detail[:500],
        )
    finally:
        if proc is not None:
            if proc.stdin is not None:
                proc.stdin.close()
            if proc.returncode is None:
                if identity is not None:
                    await asyncio.to_thread(terminate_process_tree, identity, grace=0.25)
                else:
                    proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2)
        if stderr_task is not None:
            # The process is terminated above, so stderr has hit EOF and the drain finishes on its
            # own; cancel only guards a stuck read. No raise here — a finally that raises would mask
            # the real result (the drain already bounds and redacts its retained tail).
            if not stderr_task.done():
                stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stderr_task


_CLAUDE_ALIASES = (
    ("default", "Use Claude Code account default"),
    ("best", "Most capable model available to this account"),
    ("sonnet", "Latest recommended Sonnet alias"),
    ("opus", "Latest recommended Opus alias"),
    ("haiku", "Fast Haiku alias"),
    ("sonnet[1m]", "Sonnet extended-context alias"),
    ("opus[1m]", "Opus extended-context alias"),
    ("opusplan", "Opus planning, Sonnet execution"),
)


def _read_settings(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def claude_setting_paths(
    *, home: Path, project_root: Path | None = None, platform: str | None = None
) -> list[Path]:
    platform = sys.platform if platform is None else platform
    paths = [home / ".claude" / "settings.json"]
    if project_root is not None:
        paths.append(project_root / ".claude" / "settings.json")
    if platform.startswith("win"):
        program_data = Path(os.environ.get("PROGRAMDATA", "C:/ProgramData"))
        paths.append(program_data / "ClaudeCode" / "managed-settings.json")
    elif platform == "darwin":
        paths.append(Path("/Library/Application Support/ClaudeCode/managed-settings.json"))
    else:
        paths.append(Path("/etc/claude-code/managed-settings.json"))
    return paths


AnthropicPageFetcher = Callable[[str, str, str | None], dict[str, Any]]


@dataclass(frozen=True)
class GatewayAuthPlan:
    """How to authenticate a model-discovery request to a *gateway* — a distinct model from direct
    Anthropic API auth (spec §11.3).

    Anthropic's own ``/v1/models`` authenticates only with an ``x-api-key`` API key. A gateway may
    instead expect an ``Authorization: Bearer`` OAuth/session token. Conflating the two is exactly
    what sent an OAuth token as ``x-api-key``; keeping the header and its token source together here
    makes each request carry the one credential its endpoint actually accepts.
    """

    header_name: str
    token: str


def fetch_anthropic_model_page(
    base_url: str, api_key: str, cursor: str | None, *, auth: GatewayAuthPlan | None = None
) -> dict[str, Any]:
    params = {"after_id": cursor} if cursor else {}
    url = base_url.rstrip("/") + "/v1/models"
    # x-api-key is the direct-Anthropic credential and only ever an API key; a gateway supplies its
    # own header/token via ``auth`` so an OAuth token is never sent as x-api-key (§11.2).
    headers = {"anthropic-version": "2023-06-01"}
    if auth is not None:
        headers[auth.header_name] = auth.token
    else:
        headers["x-api-key"] = api_key
    with httpx.Client(timeout=DISCOVERY_TIMEOUT_SECONDS, follow_redirects=False) as client:
        with client.stream(
            "GET",
            url,
            params=params,
            headers=headers,
        ) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_MODEL_BODY_BYTES:
                    raise ValueError("Anthropic model catalog exceeded body limit")
    value = json.loads(bytes(body))
    if not isinstance(value, dict):
        raise ValueError("Anthropic model catalog is not an object")
    return value


def _api_model_options(
    *,
    page_fetch: Callable[[str | None], dict[str, Any]],
    source: str,
) -> list[CliModelOption]:
    options: list[CliModelOption] = []
    seen: set[str] = set()
    cursor: str | None = None
    for _page in range(100):
        payload = page_fetch(cursor)
        data = payload.get("data")
        if not isinstance(data, list):
            raise ValueError("model endpoint omitted data list")
        for raw in data:
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                raise ValueError("model endpoint returned malformed entry")
            model_id = raw["id"]
            if model_id in seen:
                continue
            seen.add(model_id)
            options.append(
                CliModelOption(
                    id=model_id,
                    display_name=str(raw.get("display_name") or model_id),
                    source=source,
                    advertised=True,
                    entitlement_verified=True,
                    kind="api-model" if source == "anthropic-api" else "gateway-model",
                )
            )
        has_more = bool(payload.get("has_more"))
        next_cursor = payload.get("last_id") or payload.get("next_cursor")
        if not has_more or not next_cursor:
            return options
        cursor = str(next_cursor)
    raise ValueError("model endpoint pagination exceeded 100 pages")


def discover_claude_models(
    *,
    home: Path | None = None,
    project_root: Path | None = None,
    env: Mapping[str, str] | None = None,
    api_key: str | None = None,
    oauth_token: str | None = None,
    base_url: str | None = None,
    fetcher: AnthropicPageFetcher = fetch_anthropic_model_page,
) -> CliModelDiscoveryResult:
    """Layer documented aliases/config over optional credential-scoped /v1/models discovery.

    ``api_key`` is an Anthropic API key used as ``x-api-key`` against the direct API. ``oauth_token``
    is a distinct OAuth/session credential; it is **never** sent as ``x-api-key`` (§11.2). A gateway
    uses whichever it has via a :class:`GatewayAuthPlan`.
    """

    home = Path.home() if home is None else home
    env = dict(os.environ if env is None else env)
    options: list[CliModelOption] = [
        CliModelOption(
            id=alias,
            display_name=alias,
            source="claude-config",
            advertised=True,
            entitlement_verified=False,
            kind="alias",
            description=description,
        )
        for alias, description in _CLAUDE_ALIASES
    ]
    seen = {option.id for option in options}
    settings: dict[str, Any] = {}
    settings_env: dict[str, str] = {}
    for path in claude_setting_paths(home=home, project_root=project_root):
        layer = _read_settings(path)
        configured_env = layer.pop("env", None)
        if isinstance(configured_env, dict):
            settings_env.update(
                {
                    str(key): str(value)
                    for key, value in configured_env.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
            )
        settings.update(layer)
    if settings_env:
        # Claude Code supports model routing variables under settings.json's documented ``env``
        # object. Process environment wins, matching the CLI's own precedence.
        env = {**settings_env, **env}
    configured: list[tuple[str, str]] = []
    model = env.get("ANTHROPIC_MODEL") or settings.get("model")
    if isinstance(model, str) and model:
        configured.append((model, "configured-model"))
    available = settings.get("availableModels")
    if isinstance(available, list):
        configured.extend(
            (entry, "policy-allowed") for entry in available if isinstance(entry, str)
        )
    for variable in (
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    ):
        value = env.get(variable)
        if value:
            configured.append((value, "configured-model"))
    for model_id, kind in configured:
        if model_id in seen:
            continue
        seen.add(model_id)
        options.append(
            CliModelOption(
                id=model_id,
                display_name=model_id,
                source="claude-config",
                advertised=True,
                entitlement_verified=False,
                kind=kind,
            )
        )

    custom_model = env.get("ANTHROPIC_CUSTOM_MODEL_OPTION")
    if custom_model and custom_model not in seen:
        seen.add(custom_model)
        options.append(
            CliModelOption(
                id=custom_model,
                display_name=env.get("ANTHROPIC_CUSTOM_MODEL_OPTION_NAME") or custom_model,
                source="claude-config",
                advertised=True,
                entitlement_verified=False,
                kind="configured-model",
                description=env.get("ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION"),
            )
        )

    partial = False
    error: str | None = None
    method = "aliases + account settings"

    def _merge(page_fetch: Callable[[str | None], dict[str, Any]], source: str) -> None:
        nonlocal partial, error, method
        try:
            for option in _api_model_options(page_fetch=page_fetch, source=source):
                if option.id not in seen:
                    seen.add(option.id)
                    options.append(option)
            method += f" + {source} /v1/models"
        except Exception as exc:
            partial = True
            error = str(exc)[:500]

    if api_key or oauth_token:
        effective_base = base_url or env.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
        parsed = urlparse(effective_base)
        direct_anthropic = parsed.hostname in {"api.anthropic.com", "api.claude.com"}
        gateway_enabled = env.get("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY") == "1"
        if direct_anthropic:
            if api_key:
                key = api_key
                _merge(lambda cursor: fetcher(effective_base, key, cursor), "anthropic-api")
            else:
                # Only an OAuth/session token is present. Anthropic's /v1/models authenticates with
                # x-api-key, and an OAuth token must never be sent there (§11.2) — so it is simply not
                # usable for enumeration. Not an error: the alias/settings list still stands.
                method += " (set ANTHROPIC_API_KEY to enumerate /v1/models; OAuth token not usable)"
        elif gateway_enabled:
            # A gateway authenticates on its own terms: prefer the OAuth bearer, else the API key.
            plan = (
                GatewayAuthPlan("Authorization", f"Bearer {oauth_token}")
                if oauth_token
                else GatewayAuthPlan("x-api-key", api_key or "")
            )
            _merge(
                lambda cursor: fetch_anthropic_model_page(effective_base, "", cursor, auth=plan),
                "claude-gateway",
            )
        elif base_url:
            partial = True
            error = "custom base URL is not Anthropic and gateway discovery is not enabled"
    return CliModelDiscoveryResult(
        cli_type="claude",
        available=True,
        options=options,
        method=method,
        partial=partial,
        error=error,
    )


_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def parse_agy_models(stdout: str) -> list[str]:
    """Strip terminal controls and stable-dedupe exact model labels accepted by ``--model``."""

    models: list[str] = []
    seen: set[str] = set()
    for raw_line in stdout.splitlines():
        line = _ANSI.sub("", raw_line)
        line = "".join(char for char in line if char in "\t" or ord(char) >= 32).strip()
        line = re.sub(r"^[•*\-]\s+", "", line)
        if line and line not in seen:
            seen.add(line)
            models.append(line)
    return models


def discover_agy_models(
    executable: str, *, runner: CommandRunner = run_bounded
) -> CliModelDiscoveryResult:
    try:
        result = runner([executable, "models"], DISCOVERY_TIMEOUT_SECONDS, MAX_MODEL_BODY_BYTES)
    except Exception as exc:
        return CliModelDiscoveryResult(
            cli_type="antigravity",
            available=False,
            method="agy models",
            error=f"agy models execution failed: {str(exc)[:500]}",
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}")[:500]
        return CliModelDiscoveryResult(
            cli_type="antigravity",
            available=False,
            method="agy models",
            error=f"agy models failed: {detail}",
        )
    models = parse_agy_models(result.stdout)
    if not models:
        return CliModelDiscoveryResult(
            cli_type="antigravity",
            available=True,
            method="agy models",
            options=[],
            error=None,
        )
    return CliModelDiscoveryResult(
        cli_type="antigravity",
        available=True,
        method="agy models",
        options=[
            CliModelOption(
                id=model,
                display_name=model,
                source="agy-models",
                advertised=True,
                entitlement_verified=True,
            )
            for model in models
        ],
    )
