"""Managers for the two providers the user runs themselves (spec §12.4, §13.4).

Ollama and LM Studio are different from every other provider here in one way that matters: OpenAgent
can *change* them. It can start a daemon, load a model into RAM, download several gigabytes of weights.
Each of those is someone else's machine, someone else's disk, and someone else's evening.

So the rule this module exists to enforce: **nothing that changes the machine happens without explicit
approval, and approval is a parameter, not a default.** Every mutating method takes ``approved`` and
raises when it is False. That is deliberately more annoying than a config flag — a flag gets set once
and forgotten, whereas a parameter has to be passed by the code path that has a user in front of it.

What the managers *do* freely is look: which binary is installed and where, whether the server answers,
which models are on disk, which are resident, how much memory they are holding. Reading is safe and it
is what Doctor and the wizard need.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import ErrorType
from .spec import ProviderSpec, get_spec
from .transport import Transport, TransportError

#: How long a version/status subprocess may take before it is abandoned. Short: these are local and a
#: hang here blocks Doctor.
_PROBE_TIMEOUT = 10.0


class ApprovalRequiredError(PermissionError):
    """A mutating local-provider action was attempted without explicit approval."""

    def __init__(self, action: str) -> None:
        super().__init__(
            f"{action} changes the user's machine and needs explicit approval; "
            f"OpenAgent does not do this on its own"
        )
        self.action = action


@dataclass
class LocalModel:
    """One model on the local machine."""

    id: str
    size_bytes: int | None = None
    loaded: bool = False
    #: Bytes of memory the model is holding right now, when the server reports it.
    resident_bytes: int | None = None
    quantization: str | None = None
    parameter_size: str | None = None
    context_window: int | None = None


@dataclass
class LocalProviderStatus:
    """Everything Doctor and the wizard show about a local provider (spec §21.3)."""

    provider_type: str
    label: str
    #: The CLI/app binary, if it is on PATH.
    binary_installed: bool = False
    binary_path: str | None = None
    binary_version: str | None = None
    #: Whether the HTTP server answered.
    server_reachable: bool = False
    server_version: str | None = None
    server_url: str = ""
    models: list[LocalModel] = field(default_factory=list)
    #: Reason the server could not be reached, classified. ``None`` when it was.
    error_type: ErrorType | None = None
    detail: str = ""

    @property
    def loaded_models(self) -> list[LocalModel]:
        return [model for model in self.models if model.loaded]

    @property
    def resident_bytes(self) -> int | None:
        """Total memory the loaded models are holding, or ``None`` if nothing reported it."""

        values = [m.resident_bytes for m in self.loaded_models if m.resident_bytes is not None]
        return sum(values) if values else None

    @property
    def usable(self) -> bool:
        """Whether an agent could actually run right now."""

        return self.server_reachable and bool(self.models)

    def summary(self) -> str:
        if not self.server_reachable:
            if not self.binary_installed:
                return f"{self.label} is not installed"
            return f"{self.label} is installed but its server is not answering at {self.server_url}"
        if not self.models:
            return f"{self.label} is running with no models downloaded"
        loaded = len(self.loaded_models)
        return (
            f"{self.label} is running: {len(self.models)} model(s) on disk, "
            f"{loaded} loaded in memory"
        )

    def remediation(self) -> list[str]:
        """What the *user* can do. Never commands OpenAgent intends to run for them."""

        if not self.binary_installed and not self.server_reachable:
            return [_INSTALL_HINTS.get(self.provider_type, f"install {self.label}")]
        if not self.server_reachable:
            return [_START_HINTS.get(self.provider_type, f"start {self.label}")]
        if not self.models:
            return [_PULL_HINTS.get(self.provider_type, f"download a model for {self.label}")]
        return []


_INSTALL_HINTS = {
    "ollama": "install Ollama from https://ollama.com/download",
    "lmstudio": "install LM Studio from https://lmstudio.ai",
}
_START_HINTS = {
    "ollama": "start the Ollama app, or run `ollama serve`",
    "lmstudio": "start LM Studio's local server (Developer tab), or run `lms server start`",
}
_PULL_HINTS = {
    "ollama": "download a model, e.g. `ollama pull qwen3:8b`",
    "lmstudio": "download a model in LM Studio, or run `lms get <model>`",
}


class LocalProviderManager:
    """Shared read-only inspection plus the approval gate. Subclasses supply the provider's shapes."""

    executable: str = ""
    provider_type: str = ""

    def __init__(self, *, base_url: str | None = None, transport: Transport | None = None) -> None:
        spec = get_spec(self.provider_type)
        if spec is None:  # pragma: no cover - subclasses name a registered provider
            raise KeyError(f"{self.provider_type!r} is not a registered provider")
        self.spec: ProviderSpec = spec
        _, default_url = spec.resolve(region_id="local")
        self.base_url = (base_url or default_url).rstrip("/")
        self._transport = transport

    def transport(self) -> Transport:
        if self._transport is None:
            self._transport = Transport(
                base_url=self.base_url,
                headers={"Content-Type": "application/json"},
                # A local server that is down should be reported in a second, not after three
                # backoffs: the answer is "it is not running", and waiting does not improve it.
                max_retries=0,
                total_timeout=_PROBE_TIMEOUT,
            )
        return self._transport

    # ------------------------------------------------------------------ inspection

    def find_binary(self) -> str | None:
        return shutil.which(self.executable) if self.executable else None

    async def binary_version(self) -> str | None:
        """Ask the installed binary its version. ``None`` on any failure — never a guess."""

        path = self.find_binary()
        if path is None:
            return None
        try:
            process = await asyncio.create_subprocess_exec(
                path,
                *self._version_args(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # A minimal environment: the version probe has no business inheriting the user's
                # provider credentials (spec §19.2).
                env=_minimal_env(),
            )
        except (OSError, ValueError):
            return None
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=_PROBE_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError):
            process.kill()
            await process.wait()
            return None
        text = (stdout or b"").decode("utf-8", "replace") + (stderr or b"").decode(
            "utf-8", "replace"
        )
        return self._parse_version(text)

    def _version_args(self) -> tuple[str, ...]:
        return ("--version",)

    def _parse_version(self, text: str) -> str | None:
        import re

        match = re.search(r"\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.]+)?", text)
        return match.group(0) if match else None

    async def status(self) -> LocalProviderStatus:
        raise NotImplementedError

    # ------------------------------------------------------------------ approval gate

    @staticmethod
    def require_approval(action: str, approved: bool) -> None:
        if not approved:
            raise ApprovalRequiredError(action)

    async def _run(self, *args: str, approved: bool, action: str) -> tuple[int, str]:
        """Run a mutating command after the gate. Returns exit code and combined output."""

        self.require_approval(action, approved)
        path = self.find_binary()
        if path is None:
            raise FileNotFoundError(f"{self.executable} is not installed")
        process = await asyncio.create_subprocess_exec(
            path,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_minimal_env(),
        )
        stdout, _ = await process.communicate()
        return process.returncode or 0, (stdout or b"").decode("utf-8", "replace")


class OllamaProviderManager(LocalProviderManager):
    """Inspect and (with approval) manage a local Ollama (spec §12.4)."""

    executable = "ollama"
    provider_type = "ollama"

    async def status(self) -> LocalProviderStatus:
        path = self.find_binary()
        status = LocalProviderStatus(
            provider_type=self.provider_type,
            label=self.spec.label,
            binary_installed=path is not None,
            binary_path=path,
            server_url=self.base_url,
        )
        status.binary_version = await self.binary_version()

        transport = self.transport()
        try:
            version = await transport.get_json("/api/version")
            status.server_reachable = True
            reported = version.get("version")
            status.server_version = reported if isinstance(reported, str) else None
        except TransportError as exc:
            status.error_type = (
                ErrorType.LOCAL_SERVER_UNAVAILABLE
                if exc.error_type in {ErrorType.NETWORK_UNAVAILABLE, ErrorType.CONNECTION_LOST}
                else exc.error_type
            )
            status.detail = exc.message
            return status

        try:
            tags = await transport.get_json("/api/tags")
        except TransportError as exc:
            status.detail = f"server is up but /api/tags failed: {exc.message}"
            return status

        resident = await self._resident(transport)
        for item in tags.get("models") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("model")
            if not isinstance(name, str) or not name:
                continue
            details = item.get("details") if isinstance(item.get("details"), dict) else {}
            status.models.append(
                LocalModel(
                    id=name,
                    size_bytes=_int_or_none(item.get("size")),
                    loaded=name in resident,
                    resident_bytes=resident.get(name),
                    quantization=details.get("quantization_level"),
                    parameter_size=details.get("parameter_size"),
                )
            )
        return status

    async def _resident(self, transport: Transport) -> dict[str, int | None]:
        try:
            running = await transport.get_json("/api/ps")
        except TransportError:
            return {}
        out: dict[str, int | None] = {}
        for item in running.get("models") or []:
            if isinstance(item, dict):
                name = item.get("name") or item.get("model")
                if isinstance(name, str) and name:
                    out[name] = _int_or_none(item.get("size_vram"))
        return out

    async def pull(self, model: str, *, approved: bool = False) -> tuple[int, str]:
        """Download a model. Multi-gigabyte and irreversible in cost, so approval is mandatory."""

        return await self._run("pull", model, approved=approved, action=f"pulling {model!r}")

    async def stop(self, model: str, *, approved: bool = False) -> tuple[int, str]:
        """Evict a model from memory. Mutating: it interrupts whatever else is using it."""

        return await self._run("stop", model, approved=approved, action=f"unloading {model!r}")


class LmStudioProviderManager(LocalProviderManager):
    """Inspect and (with approval) manage a local LM Studio (spec §13.4)."""

    executable = "lms"
    provider_type = "lmstudio"

    def _version_args(self) -> tuple[str, ...]:
        return ("version",)

    async def status(self) -> LocalProviderStatus:
        path = self.find_binary()
        status = LocalProviderStatus(
            provider_type=self.provider_type,
            label=self.spec.label,
            binary_installed=path is not None,
            binary_path=path,
            server_url=self.base_url,
        )
        status.binary_version = await self.binary_version()

        transport = self.transport()
        payload: dict[str, Any] | None = None
        # The native path has moved between builds and an OpenAI-compatible one is always present, so
        # the endpoints are probed in richest-first order rather than one being assumed.
        for candidate in ("/api/v0/models", "/api/v1/models", "/v1/models"):
            try:
                payload = await transport.get_json(candidate)
                status.server_reachable = True
                break
            except TransportError as exc:
                if exc.error_type in {ErrorType.MODEL_NOT_FOUND, ErrorType.INVALID_REQUEST}:
                    continue
                status.error_type = (
                    ErrorType.LOCAL_SERVER_UNAVAILABLE
                    if exc.error_type in {ErrorType.NETWORK_UNAVAILABLE, ErrorType.CONNECTION_LOST}
                    else exc.error_type
                )
                status.detail = exc.message
                return status
        if payload is None:
            status.error_type = ErrorType.LOCAL_SERVER_UNAVAILABLE
            status.detail = "no LM Studio model endpoint answered"
            return status

        for item in payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id") or item.get("key")
            if not isinstance(model_id, str) or not model_id:
                continue
            status.models.append(
                LocalModel(
                    id=model_id,
                    loaded=item.get("state") == "loaded",
                    quantization=item.get("quantization"),
                    context_window=_int_or_none(
                        item.get("max_context_length") or item.get("context_length")
                    ),
                )
            )
        return status

    async def daemon_up(self, *, approved: bool = False) -> tuple[int, str]:
        return await self._run(
            "daemon", "up", approved=approved, action="starting the LM Studio daemon"
        )

    async def server_start(self, *, approved: bool = False) -> tuple[int, str]:
        return await self._run(
            "server", "start", approved=approved, action="starting the LM Studio server"
        )

    async def load(self, model: str, *, approved: bool = False) -> tuple[int, str]:
        """Load a model into RAM. Can consume most of the machine's memory."""

        return await self._run("load", model, approved=approved, action=f"loading {model!r}")

    async def download(self, model: str, *, approved: bool = False) -> tuple[int, str]:
        return await self._run("get", model, approved=approved, action=f"downloading {model!r}")


#: Environment for a local-provider subprocess: PATH and the platform essentials, and nothing that
#: carries a credential. A version probe has no reason to see the user's API keys, and an inherited
#: environment is how one provider's token reaches another provider's process (spec §19.2).
_ENV_ALLOWLIST = ("PATH", "HOME", "USERPROFILE", "SystemRoot", "TEMP", "TMP", "LANG", "LC_ALL")


def _minimal_env() -> dict[str, str]:
    env = {name: os.environ[name] for name in _ENV_ALLOWLIST if name in os.environ}
    env.setdefault("PATH", os.defpath)
    return env


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def managers() -> list[LocalProviderManager]:
    return [OllamaProviderManager(), LmStudioProviderManager()]
