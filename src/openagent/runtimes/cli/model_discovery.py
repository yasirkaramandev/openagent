"""Honest, documented model discovery for Codex, Claude Code, and Antigravity."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ... import __version__
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


_SCHEMA_CACHE: dict[tuple[str, str], bool] = {}


def _schema_supports_model_list(
    executable: str, version: str | None, runner: CommandRunner = run_bounded
) -> bool:
    key = (str(Path(executable).resolve()), version or "unknown")
    cached = _SCHEMA_CACHE.get(key)
    if cached is not None:
        return cached
    with tempfile.TemporaryDirectory(prefix="openagent-codex-schema-") as temporary:
        output = Path(temporary)
        try:
            result = runner(
                [executable, "app-server", "generate-json-schema", "--out", str(output)],
                DISCOVERY_TIMEOUT_SECONDS,
                MAX_SCHEMA_BYTES,
            )
            if result.returncode != 0:
                _SCHEMA_CACHE[key] = False
                return False
            total = 0
            found = False
            for path in output.rglob("*.json"):
                if not path.is_file():
                    continue
                size = path.stat().st_size
                total += size
                if total > MAX_SCHEMA_BYTES:
                    raise ValueError("Codex app-server schema exceeds size limit")
                if "model/list" in path.read_text(encoding="utf-8", errors="replace"):
                    found = True
            _SCHEMA_CACHE[key] = found
            return found
        except Exception:
            _SCHEMA_CACHE[key] = False
            return False


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


async def discover_codex_models(
    executable: str,
    *,
    version: str | None = None,
    schema_runner: CommandRunner = run_bounded,
) -> CliModelDiscoveryResult:
    """Handshake with the installed Codex app-server and page ``model/list`` without a turn."""

    schema_ok = await asyncio.to_thread(
        _schema_supports_model_list, executable, version, schema_runner
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
    stderr_task: asyncio.Task[bytes] | None = None
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
        stderr_task = asyncio.create_task(proc.stderr.read(MAX_PROTOCOL_BYTES + 1))

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
        return CliModelDiscoveryResult(
            cli_type="codex",
            available=False,
            method="codex-app-server model/list",
            error=str(exc)[:500],
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
            if not stderr_task.done():
                stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                stderr = await stderr_task
                if len(stderr) > MAX_PROTOCOL_BYTES:
                    raise ValueError("Codex app-server stderr exceeded discovery limit")


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


def fetch_anthropic_model_page(base_url: str, api_key: str, cursor: str | None) -> dict[str, Any]:
    params = {"after_id": cursor} if cursor else {}
    url = base_url.rstrip("/") + "/v1/models"
    with httpx.Client(timeout=DISCOVERY_TIMEOUT_SECONDS, follow_redirects=False) as client:
        with client.stream(
            "GET",
            url,
            params=params,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
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
    base_url: str,
    api_key: str,
    fetcher: AnthropicPageFetcher,
    source: str,
) -> list[CliModelOption]:
    options: list[CliModelOption] = []
    seen: set[str] = set()
    cursor: str | None = None
    for _page in range(100):
        payload = fetcher(base_url, api_key, cursor)
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
    base_url: str | None = None,
    fetcher: AnthropicPageFetcher = fetch_anthropic_model_page,
) -> CliModelDiscoveryResult:
    """Layer documented aliases/config over optional credential-scoped /v1/models discovery."""

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
    if api_key:
        effective_base = base_url or env.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
        parsed = urlparse(effective_base)
        direct_anthropic = parsed.hostname in {"api.anthropic.com", "api.claude.com"}
        gateway_enabled = env.get("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY") == "1"
        if direct_anthropic or gateway_enabled:
            source = "anthropic-api" if direct_anthropic else "claude-gateway"
            try:
                api_options = _api_model_options(
                    base_url=effective_base,
                    api_key=api_key,
                    fetcher=fetcher,
                    source=source,
                )
                for option in api_options:
                    if option.id not in seen:
                        seen.add(option.id)
                        options.append(option)
                method += f" + {source} /v1/models"
            except Exception as exc:
                partial = True
                error = str(exc)[:500]
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
