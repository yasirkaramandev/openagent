"""Live smoke for every v0.2 API provider (spec §26.2, §26.3).

One parameterized file rather than nine, for the same reason the offline contract suite is
parameterized: nine hand-written live tests drift, and the drift shows up as one provider quietly
testing less than the others.

**A skipped provider is `BLOCKED_BY_CREDENTIAL`, and that is not a pass.** Every skip here says so in
its reason string, and the workflow repeats it in the job summary. A green check mark on a job that
tested nothing is precisely the outcome §26.2 forbids.

**Spend control (§26.3).** Each provider gets: one minimal prompt, a 32-token ceiling, one no-op tool,
one continuation, a strict timeout, and a hard cap on how many requests the whole file may make
against one provider. Nothing loops, nothing retries beyond the transport's own bounded budget, and
no test asks for more than a sentence.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from openagent.core.errors import ErrorType
from openagent.providers.base import Message, NormalizedModelRequest, Role, collect
from openagent.providers.compat.evidence import Capability
from openagent.providers.spec import get_spec
from openagent.providers.wire_adapter import WireProviderAdapter

pytestmark = [pytest.mark.integration, pytest.mark.live_provider]

#: Which environment variable carries the key for each provider in CI. Deliberately prefixed, so a
#: developer's ordinary DEEPSEEK_API_KEY never triggers a spend from a local test run.
KEY_ENV = {
    "gemini": "OPENAGENT_LIVE_GEMINI_API_KEY",
    "deepseek": "OPENAGENT_LIVE_DEEPSEEK_API_KEY",
    "qwen": "OPENAGENT_LIVE_QWEN_API_KEY",
    "kimi": "OPENAGENT_LIVE_KIMI_API_KEY",
    "glm": "OPENAGENT_LIVE_GLM_API_KEY",
    "minimax": "OPENAGENT_LIVE_MINIMAX_API_KEY",
    "openrouter": "OPENAGENT_LIVE_OPENROUTER_API_KEY",
}

#: Local providers need no key — they need a *server*. Skipping them for an absent key would be the
#: wrong reason, so they are checked for reachability instead.
LOCAL_PROVIDERS = ("ollama", "lmstudio")

ALL_PROVIDERS = (*KEY_ENV, *LOCAL_PROVIDERS)

#: The model to smoke, per provider. Read from the environment so a key with limited model access can
#: still run, and so no model id is hardcoded into a test that would then fail when it is retired.
MODEL_ENV = {provider: f"OPENAGENT_LIVE_{provider.upper()}_MODEL" for provider in ALL_PROVIDERS}

#: One short answer. Not "write me a function": a bounded prompt keeps spend and runtime predictable,
#: and nothing here needs a long answer to prove the wire works.
PROMPT = "Reply with exactly the word: ready"
MAX_TOKENS = 32
TIMEOUT = 60.0

PING_TOOL = {
    "name": "ping",
    "description": "A no-op probe tool.",
    "parameters": {
        "type": "object",
        "properties": {"value": {"type": "integer", "description": "any integer"}},
        "required": ["value"],
    },
}


def _credential(provider: str) -> str | None:
    return os.environ.get(KEY_ENV.get(provider, ""), "") or None


def _model(provider: str) -> str | None:
    return os.environ.get(MODEL_ENV[provider], "") or None


async def _adapter(provider: str) -> WireProviderAdapter:
    """Build an adapter, or skip with the honest reason.

    The two skip reasons are kept distinct: a hosted provider with no key is
    ``BLOCKED_BY_CREDENTIAL``, and a local provider with no server is ``PROVIDER_UNAVAILABLE``. Both
    are non-passes and they mean different things to whoever reads the summary.
    """

    spec = get_spec(provider)
    assert spec is not None

    if provider in LOCAL_PROVIDERS:
        adapter = WireProviderAdapter(spec=spec, api_key=None)
        health = await asyncio.wait_for(adapter.test_connection(), TIMEOUT)
        if not health.ok:
            await adapter.aclose()
            pytest.skip(f"PROVIDER_UNAVAILABLE: {provider} — {health.detail}")
        return adapter

    key = _credential(provider)
    if not key:
        pytest.skip(
            f"BLOCKED_BY_CREDENTIAL: {KEY_ENV[provider]} is not set. This is NOT a pass — nothing ran."
        )
    return WireProviderAdapter(spec=spec, api_key=key)


async def _pick_model(adapter: WireProviderAdapter, provider: str) -> str:
    """The configured model, or the first the catalog offers. Never a hardcoded id."""

    configured = _model(provider)
    if configured:
        return configured
    catalog = await asyncio.wait_for(adapter.catalog(), TIMEOUT)
    if not catalog.entries:
        pytest.skip(
            f"CATALOG_ONLY: {provider} offered no model to smoke "
            f"({catalog.summary()}); set {MODEL_ENV[provider]} to name one."
        )
    return catalog.entries[0].model.id


def _request(model: str, **kwargs) -> NormalizedModelRequest:
    fields = {
        "model": model,
        "messages": [Message(role=Role.USER, content=PROMPT)],
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    fields.update(kwargs)
    return NormalizedModelRequest(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_connection(provider: str) -> None:
    """Reachable, and the credential is accepted. One request."""

    adapter = await _adapter(provider)
    try:
        health = await asyncio.wait_for(adapter.test_connection(), TIMEOUT)
        assert health.ok, f"{provider} connection failed: {health.detail}"
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_catalog_is_readable_or_honestly_manual(provider: str) -> None:
    """Either the catalog reads, or the provider says it has none. An empty success is neither."""

    adapter = await _adapter(provider)
    try:
        catalog = await asyncio.wait_for(adapter.catalog(refresh=True), TIMEOUT)
        if catalog.manual_only:
            assert catalog.ok, "a manual-only catalog is a configuration, not a failure"
            return
        assert catalog.ok or catalog.partial, f"{provider}: {catalog.summary()}"
        assert catalog.entries, f"{provider} returned an empty catalog with ok=True"
        ids = [entry.model.id for entry in catalog.entries]
        assert len(ids) == len(set(ids)), "the catalog contained duplicate model ids"
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_a_minimal_text_turn(provider: str) -> None:
    """One non-streaming turn, 32 tokens. The cheapest proof the wire is right."""

    adapter = await _adapter(provider)
    try:
        model = await _pick_model(adapter, provider)
        result = await asyncio.wait_for(collect(adapter.stream_response(_request(model))), TIMEOUT)
        if result.is_error:
            _fail_or_skip(provider, result.error_type, result.error_message)
        assert result.text.strip(), f"{provider}/{model} produced no text"
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_a_minimal_streamed_turn(provider: str) -> None:
    """The same turn, streamed. This is what exercises the SSE/NDJSON framing for real."""

    adapter = await _adapter(provider)
    try:
        model = await _pick_model(adapter, provider)
        result = await asyncio.wait_for(
            collect(adapter.stream_response(_request(model, stream=True))), TIMEOUT
        )
        if result.is_error:
            _fail_or_skip(provider, result.error_type, result.error_message)
        assert result.text.strip(), f"{provider}/{model} streamed no text"
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_one_no_op_tool_call(provider: str) -> None:
    """One tool-enabled request. A model that declines is not a failure — it is unproven."""

    adapter = await _adapter(provider)
    try:
        model = await _pick_model(adapter, provider)
        request = _request(
            model,
            messages=[Message(role=Role.USER, content="Call the ping tool with value 1.")],
            tools=[PING_TOOL],
            max_tokens=64,
        )
        result = await asyncio.wait_for(collect(adapter.stream_response(request)), TIMEOUT)
        if result.is_error:
            _fail_or_skip(provider, result.error_type, result.error_message)
        if not result.tool_calls:
            pytest.skip(
                f"CATALOG_ONLY: {provider}/{model} did not call the tool. Declining is not proof "
                f"it cannot, so nothing is asserted either way."
            )
        call = result.tool_calls[0]
        assert call.name == "ping"
        assert isinstance(call.arguments, dict)
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_one_continuation(provider: str) -> None:
    """A second turn built on the first. The path a resume actually takes."""

    adapter = await _adapter(provider)
    try:
        model = await _pick_model(adapter, provider)
        first = await asyncio.wait_for(collect(adapter.stream_response(_request(model))), TIMEOUT)
        if first.is_error:
            _fail_or_skip(provider, first.error_type, first.error_message)

        envelope = adapter.build_continuation(model_id=model)
        # The envelope must be bound to what produced it, whatever strategy it chose.
        assert envelope.provider_type == adapter.spec.provider_type
        assert envelope.protocol is adapter.protocol
        warnings = envelope.verify(
            provider_type=adapter.spec.provider_type,
            protocol=adapter.protocol,
            model_id=model,
        )
        assert warnings == []

        follow_up = _request(
            model,
            messages=[
                Message(role=Role.USER, content=PROMPT),
                Message(role=Role.ASSISTANT, content=first.text),
                Message(role=Role.USER, content="Reply with exactly the word: again"),
            ],
        )
        second = await asyncio.wait_for(collect(adapter.stream_response(follow_up)), TIMEOUT)
        if second.is_error:
            _fail_or_skip(provider, second.error_type, second.error_message)
        assert second.text.strip()
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_capability_probe_records_only_what_it_saw(provider: str) -> None:
    """The probe's own honesty, against a real endpoint."""

    adapter = await _adapter(provider)
    try:
        model = await _pick_model(adapter, provider)
        outcome = await asyncio.wait_for(adapter.probe_evidence(model), TIMEOUT * 3)
        if not outcome.ran:
            pytest.skip(
                f"BLOCKED_BY_CREDENTIAL: {provider} probe could not run "
                f"({outcome.blocked_by.value if outcome.blocked_by else 'unknown'}): {outcome.detail}"
            )
        # Text is the one capability the probe always establishes one way or the other.
        assert outcome.ledger.supports(Capability.TEXT) is not None
        # And nothing it did not exercise may be marked unsupported.
        assert outcome.ledger.supports(Capability.AUDIO_INPUT) is None
    finally:
        await adapter.aclose()


def _fail_or_skip(provider: str, error_type: str | None, message: str | None) -> None:
    """Distinguish "the adapter is wrong" from "this account cannot run the test".

    A rate limit or a spent balance is not an adapter regression, and failing the job for it would
    train people to ignore this workflow. Everything else fails, loudly.
    """

    blocked = {
        ErrorType.PROVIDER_RATE_LIMITED.value,
        ErrorType.INSUFFICIENT_BALANCE.value,
        ErrorType.PROVIDER_OVERLOADED.value,
        ErrorType.MODEL_NOT_FOUND.value,
        ErrorType.MODEL_CAPABILITY_MISSING.value,
    }
    if error_type in blocked:
        pytest.skip(f"PROVIDER_UNAVAILABLE: {provider} — {error_type}: {message}")
    if error_type in {
        ErrorType.AUTHENTICATION_FAILED.value,
        ErrorType.PERMISSION_DENIED.value,
        ErrorType.PROVIDER_REGION_MISMATCH.value,
    }:
        pytest.skip(f"BLOCKED_BY_CREDENTIAL: {provider} — {error_type}: {message}")
    pytest.fail(f"{provider} returned {error_type}: {message}")
