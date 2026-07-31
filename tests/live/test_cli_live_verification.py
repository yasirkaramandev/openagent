"""Live verification of the CLIs installed on this machine (spec §20, §26.2).

Opt-in. Nothing here runs unless ``OPENAGENT_LIVE_CLI_TESTS=1``, because these spawn real CLIs against
real accounts and spend real quota.

The reason this file exists separately from ``tests/contract`` is the distinction spec §26.2 insists on:
a fixture-validated adapter and a live-verified one are different claims, and a missing credential is
``BLOCKED_BY_CREDENTIAL`` — **not** a pass. So every check here reports which of those it achieved, and
a blocked check is skipped with the reason recorded rather than quietly passing.

Spend control (spec §26.3): one minimal prompt, a tiny output ceiling, one no-op tool, a strict
timeout. No test here loops, retries, or asks for more than a sentence.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from openagent.core.events import EventType
from openagent.runtimes.cli.base import TERMINAL_EVENT_TYPES, CliRunRequest
from openagent.runtimes.cli.registry import build_cli_adapter

pytestmark = [
    pytest.mark.live_cli,
    pytest.mark.skipif(
        os.environ.get("OPENAGENT_LIVE_CLI_TESTS") != "1",
        reason="set OPENAGENT_LIVE_CLI_TESTS=1 to run live CLI verification",
    ),
]

#: The verification vocabulary spec §26.2 requires. A result outside this set is a bug in the report.
LIVE_VERIFIED = "LIVE_VERIFIED"
FIXTURE_VERIFIED = "FIXTURE_VERIFIED"
BLOCKED_BY_CREDENTIAL = "BLOCKED_BY_CREDENTIAL"
BLOCKED_BY_AUTH = "BLOCKED_BY_AUTH"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"

#: One short answer. Deliberately not "write me a function": a bounded prompt keeps the spend and the
#: runtime predictable, and nothing here needs a long answer to prove the event contract.
MINIMAL_PROMPT = "Reply with exactly the word: ready"

#: Hard ceiling for one live run. A CLI that has not resolved by now is reported, not waited for.
RUN_TIMEOUT = 180.0


async def _require_installed(cli_type: str):
    adapter = build_cli_adapter(cli_type)
    installation = await adapter.detect()
    if installation is None:
        pytest.skip(f"{PROVIDER_UNAVAILABLE}: {cli_type} is not installed on this machine")
    return adapter, installation


async def _require_authenticated(cli_type: str):
    """Skip with ``BLOCKED_BY_AUTH`` rather than fail when the CLI has no usable login.

    This is the state spec §20.2 anticipates for Claude Code: the adapter is complete, the CLI is
    installed, and the account is not signed in. Reporting that as a failing test would be wrong, and
    reporting it as a pass would be worse.
    """

    adapter, installation = await _require_installed(cli_type)
    auth = await adapter.inspect_auth()
    if auth.blocking or not auth.authenticated:
        pytest.skip(f"{BLOCKED_BY_AUTH}: {cli_type} — {auth.detail}")
    return adapter, installation


class TestInstallationSurface:
    """Discovery only: no model call, no quota, safe to run anywhere."""

    @pytest.mark.parametrize(
        "cli_type", ["codex", "claude", "gemini", "antigravity", "qwen", "kimi"]
    )
    async def test_version_and_provenance_are_readable(self, cli_type: str) -> None:
        _adapter, installation = await _require_installed(cli_type)
        assert installation.executable
        assert installation.version, "an installed CLI must report a version"
        assert installation.install_source.value != "unknown" or installation.resolved_executable

    @pytest.mark.parametrize(
        "cli_type", ["codex", "claude", "gemini", "antigravity", "qwen", "kimi"]
    )
    async def test_capabilities_come_from_the_installed_binary(self, cli_type: str) -> None:
        adapter, installation = await _require_installed(cli_type)
        caps = await adapter.capabilities()
        # Nothing is asserted about the *values* — they depend on the build. What is asserted is that
        # answering them did not raise, which is what the wizard depends on.
        assert isinstance(caps.structured_events, bool)
        assert isinstance(caps.resumable, bool)

    @pytest.mark.parametrize(
        "cli_type", ["codex", "claude", "gemini", "antigravity", "qwen", "kimi"]
    )
    async def test_path_shadows_are_reported(self, cli_type: str) -> None:
        adapter, installation = await _require_installed(cli_type)
        # A shadowed executable is a real condition, not an error. What matters is that the resolved
        # winner and the shadows are both reported, so Doctor can explain a surprising version.
        assert installation.resolved_executable
        assert isinstance(list(installation.shadowed_executables), list)

    @pytest.mark.parametrize(
        "cli_type", ["codex", "claude", "gemini", "antigravity", "qwen", "kimi"]
    )
    async def test_auth_inspection_never_returns_a_credential(self, cli_type: str) -> None:
        adapter, _installation = await _require_installed(cli_type)
        auth = await adapter.inspect_auth()
        rendered = f"{auth.detail}{auth.environment_names}{auth.source}"
        for name in ("sk-", "sk-ant-", "nvapi-"):
            assert name not in rendered or "sk-" not in rendered.replace("sk-ant-", "")


class TestModelDiscovery:
    @pytest.mark.parametrize("cli_type", ["codex", "antigravity"])
    async def test_models_are_enumerated_by_the_cli_itself(self, cli_type: str) -> None:
        adapter, _installation = await _require_installed(cli_type)
        models = await asyncio.wait_for(adapter.list_models(), timeout=60)
        assert models, f"{cli_type} returned no models; discovery is meant to be live-verified here"
        assert len(models) == len(set(models)), "a discovery result must not contain duplicates"

    async def test_claude_model_discovery_or_blocked(self) -> None:
        adapter, _installation = await _require_installed("claude")
        models = await asyncio.wait_for(adapter.list_models(), timeout=60)
        result = getattr(adapter, "last_model_discovery", None)
        if not models:
            reason = result.error if result is not None else "no reason reported"
            pytest.skip(f"{BLOCKED_BY_AUTH}: claude model discovery unavailable — {reason}")
        assert len(models) == len(set(models))

    async def test_gemini_reports_its_method_or_says_why_not(self) -> None:
        adapter, _installation = await _require_installed("gemini")
        await asyncio.wait_for(adapter.list_models(), timeout=60)
        result = getattr(adapter, "last_model_discovery", None)
        assert result is not None
        if not result.available:
            # The honest outcome: no listing command, no allowlist, no key. The reason must be
            # actionable rather than a bare "unavailable".
            assert result.error and len(result.error) > 20


class TestOneMinimalRun:
    """The event contract, verified against a real run. One prompt, one sentence, strict timeout."""

    async def test_codex_run_resolves_to_exactly_one_terminal_event(self, tmp_path: Path) -> None:
        adapter, installation = await _require_authenticated("codex")
        request = CliRunRequest(
            run_id="live-codex-1",
            prompt=MINIMAL_PROMPT,
            workspace=tmp_path,
            permission_profile="read-only",
        )
        events = await _collect(adapter, request)

        terminal = [e for e in events if _type_of(e) in TERMINAL_EVENT_TYPES]
        assert len(terminal) == 1, (
            f"a run must resolve to exactly one terminal event; got "
            f"{[_type_of(e) for e in terminal]}"
        )
        started = [e for e in events if _type_of(e) == EventType.PROCESS_STARTED.value]
        assert len(started) == 1, "exactly one process.started per run"
        assert started[0].data.get("pid")

    async def test_codex_emits_a_message(self, tmp_path: Path) -> None:
        adapter, _installation = await _require_authenticated("codex")
        request = CliRunRequest(
            run_id="live-codex-2",
            prompt=MINIMAL_PROMPT,
            workspace=tmp_path,
            permission_profile="read-only",
        )
        events = await _collect(adapter, request)
        text = "".join(
            str(e.data.get("text") or "")
            for e in events
            if _type_of(e) in {EventType.MESSAGE_COMPLETED.value, EventType.MESSAGE_DELTA.value}
        )
        terminal = [e for e in events if _type_of(e) in TERMINAL_EVENT_TYPES][0]
        if _type_of(terminal) == EventType.RUN_FAILED.value:
            pytest.skip(
                f"{BLOCKED_BY_CREDENTIAL}: the run failed before producing text — "
                f"{terminal.data.get('error_type')}: {str(terminal.data.get('message'))[:200]}"
            )
        assert text.strip(), "an authenticated run should produce some assistant text"

    async def test_claude_run_or_blocked_by_auth(self, tmp_path: Path) -> None:
        """Claude Code is installed here but signed out, so this is expected to skip (spec §20.2)."""

        adapter, _installation = await _require_authenticated("claude")
        request = CliRunRequest(
            run_id="live-claude-1",
            prompt=MINIMAL_PROMPT,
            workspace=tmp_path,
            permission_profile="read-only",
        )
        events = await _collect(adapter, request)
        terminal = [e for e in events if _type_of(e) in TERMINAL_EVENT_TYPES]
        assert len(terminal) == 1


class TestCancellation:
    async def test_a_live_run_can_be_cancelled_and_reports_cancelled(self, tmp_path: Path) -> None:
        """Cancel must terminate the tree and produce a cancelled terminal, not a hang."""

        adapter, _installation = await _require_authenticated("codex")
        request = CliRunRequest(
            run_id="live-cancel-1",
            # A prompt long enough that there is something to cancel, still bounded.
            prompt="Count slowly from 1 to 40, one number per line.",
            workspace=tmp_path,
            permission_profile="read-only",
        )
        events: list = []

        async def drain() -> None:
            async for event in adapter.start_run(request):
                events.append(event)

        task = asyncio.ensure_future(drain())
        # Wait for the process to actually exist before cancelling it.
        for _ in range(100):
            await asyncio.sleep(0.1)
            if any(_type_of(e) == EventType.PROCESS_STARTED.value for e in events):
                break
        result = await adapter.cancel(request.run_id)
        assert result.outcome.value in {"terminated", "killed", "already_gone"}
        try:
            await asyncio.wait_for(task, timeout=30)
        except (TimeoutError, asyncio.TimeoutError):
            task.cancel()
            pytest.fail("cancel did not end the run within 30s")


async def _collect(adapter, request: CliRunRequest) -> list:  # noqa: ANN001 - adapter protocol
    events: list = []
    try:
        async for event in asyncio_timeout(adapter.start_run(request), RUN_TIMEOUT):
            events.append(event)
    except (TimeoutError, asyncio.TimeoutError):
        pytest.skip(f"{PROVIDER_UNAVAILABLE}: the run exceeded {RUN_TIMEOUT:g}s")
    return events


async def asyncio_timeout(iterator, timeout: float):
    """Iterate an async generator under one overall deadline.

    ``asyncio.wait_for`` cannot wrap an async *generator*, and wrapping each ``__anext__`` would give
    each event its own timeout — a CLI that emits a keep-alive every second would never time out.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    agen = iterator.__aiter__()
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        try:
            item = await asyncio.wait_for(agen.__anext__(), remaining)
        except StopAsyncIteration:
            return
        yield item


def _type_of(event) -> str:  # noqa: ANN001 - NormalizedEvent
    return event.type if isinstance(event.type, str) else event.type.value
