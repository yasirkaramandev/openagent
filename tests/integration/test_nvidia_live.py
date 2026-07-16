"""Opt-in LIVE NVIDIA Build test (spec §21).

Runs only when a real key is present **and** the user opts in::

    export NVIDIA_API_KEY=...
    export OPENAGENT_LIVE_NVIDIA=1
    export OPENAGENT_NVIDIA_TEST_MODEL=<publisher/model>   # optional; else the first chat-ish entry
    pytest tests/integration/test_nvidia_live.py -v

Without those it SKIPS — it never reports "passed live" for a run that never touched NVIDIA. The
offline contract/fixture suite (``tests/contract/test_nvidia_build.py``) is always mandatory and does
not depend on this. No model id is hardcoded: catalogs rotate, so the model is discovered or supplied
via the environment.
"""

from __future__ import annotations

import os

import pytest

from openagent.providers.compat.profiles import get_compat
from openagent.providers.discovery import (
    PROBE_VERIFIED,
    filter_models,
    looks_non_chat,
    probe_agent_model,
)
from openagent.providers.factory import get_preset
from openagent.providers.openai_chat import OpenAIChatAdapter

_KEY = os.environ.get("NVIDIA_API_KEY")
_OPTED_IN = os.environ.get("OPENAGENT_LIVE_NVIDIA") == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _KEY, reason="SKIPPED — NVIDIA_API_KEY not available"),
    pytest.mark.skipif(
        not _OPTED_IN,
        reason="SKIPPED — live NVIDIA test not opted in (set OPENAGENT_LIVE_NVIDIA=1)",
    ),
]


def _adapter() -> OpenAIChatAdapter:
    preset = get_preset("nvidia-build")
    assert preset is not None and preset.openai_base_url
    return OpenAIChatAdapter(
        base_url=preset.openai_base_url,
        api_key=_KEY,
        provider_type="nvidia-build",
        compat=get_compat("nvidia-build"),
    )


async def test_live_catalog_probe_and_no_key_leak(capsys: pytest.CaptureFixture[str]) -> None:
    adapter = _adapter()
    try:
        # 1) The catalog is reachable and returns entries.
        models = await adapter.list_models()
        assert models, "the live NVIDIA catalog returned no models"

        # 2) Choose the model to validate — from the env, or the first entry that does not *look*
        #    like a non-chat model (a hint only; the probe below is the real authority).
        wanted = os.environ.get("OPENAGENT_NVIDIA_TEST_MODEL")
        if wanted:
            assert any(m.id == wanted for m in models), (
                f"{wanted!r} is not in the live catalog — pick a current model"
            )
            model_id = wanted
        else:
            candidates = [m for m in models if not looks_non_chat(m.id)]
            assert candidates, "no plausible chat model found in the live catalog"
            model_id = candidates[0].id

        # 3-5) A real capability probe: text, streaming, and tool calling.
        probe = await probe_agent_model(adapter, model_id)
        assert probe.capabilities.text is True, (
            f"{model_id} produced no text: {probe.category} — {probe.message()}"
        )
        # Streaming/tool-calling are reported honestly; only a fully verified model is agent ready.
        if probe.category != PROBE_VERIFIED:
            pytest.skip(
                f"live model {model_id} is not fully agent-compatible ({probe.category}); "
                "text was verified. Set OPENAGENT_NVIDIA_TEST_MODEL to a tool-capable model."
            )

        # Publisher metadata survives, so the catalog can be filtered.
        assert filter_models(models, search=model_id.split("/")[-1])
    finally:
        await adapter.transport.aclose()

    # 6) The key must never appear in anything this test produced.
    captured = capsys.readouterr()
    assert _KEY not in captured.out
    assert _KEY not in captured.err
