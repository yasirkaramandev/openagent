from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from openagent.runtimes.cli.locator import CommandResult
from openagent.runtimes.cli.model_discovery import (
    discover_agy_models,
    discover_claude_models,
    discover_codex_models,
    parse_agy_models,
    parse_codex_model_page,
)


def test_codex_page_preserves_effort_order_and_hides_hidden_models():
    options, cursor = parse_codex_model_page(
        {
            "data": [
                {
                    "id": "gpt-visible",
                    "displayName": "GPT Visible",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "high"},
                        {"reasoningEffort": "low"},
                        {"reasoningEffort": "medium"},
                    ],
                },
                {"id": "gpt-hidden", "hidden": True},
            ],
            "nextCursor": "page-2",
        }
    )

    assert [option.id for option in options] == ["gpt-visible"]
    assert options[0].reasoning_efforts == ["high", "low", "medium"]
    assert options[0].entitlement_verified is False
    assert cursor == "page-2"


def test_codex_app_server_handshake_notifications_and_pagination(tmp_path: Path):
    executable = tmp_path / "fake-codex"
    executable.write_text(
        f"""#!{sys.executable}
import json
import sys

for raw in sys.stdin:
    message = json.loads(raw)
    if message.get("method") == "initialize":
        print(json.dumps({{"method": "server/ready", "params": {{}}}}), flush=True)
        print(json.dumps({{"id": 1, "result": {{"serverInfo": {{"name": "fake"}}}}}}), flush=True)
    elif message.get("method") == "model/list":
        cursor = message.get("params", {{}}).get("cursor")
        if cursor:
            result = {{"data": [{{"id": "model-b", "supportedReasoningEfforts": ["max", "low"]}}]}}
        else:
            result = {{"data": [{{"id": "model-a", "supportedReasoningEfforts": ["medium", "high"]}}], "nextCursor": "next"}}
        print(json.dumps({{"id": message["id"], "result": result}}), flush=True)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    def schema_runner(argv, timeout, limit):
        del timeout, limit
        output = Path(argv[-1])
        (output / "schema.json").write_text(
            json.dumps({"methods": ["model/list"]}), encoding="utf-8"
        )
        return CommandResult(returncode=0)

    result = asyncio.run(
        discover_codex_models(str(executable), version="fake-1", schema_runner=schema_runner)
    )

    assert result.available is True
    assert result.models == ["model-a", "model-b"]
    assert result.options[0].reasoning_efforts == ["medium", "high"]
    assert result.options[1].reasoning_efforts == ["max", "low"]


def test_codex_discovery_fails_honestly_when_schema_method_is_missing(tmp_path: Path):
    executable = tmp_path / "codex"
    executable.write_text("unused", encoding="utf-8")

    def schema_runner(argv, timeout, limit):
        del timeout, limit
        Path(argv[-1], "schema.json").write_text("{}", encoding="utf-8")
        return CommandResult(returncode=0)

    result = asyncio.run(
        discover_codex_models(str(executable), version="no-list", schema_runner=schema_runner)
    )

    assert result.available is False
    assert "manual id" in (result.error or "")


def test_claude_aliases_settings_env_and_custom_labels(tmp_path: Path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps(
            {
                "model": "settings-model",
                "availableModels": ["policy-a", "policy-b"],
                "env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "opus-routed"},
            }
        ),
        encoding="utf-8",
    )

    result = discover_claude_models(
        home=tmp_path,
        env={
            "ANTHROPIC_MODEL": "env-model",
            "ANTHROPIC_CUSTOM_MODEL_OPTION": "custom-id",
            "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "My routed model",
            "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION": "Internal gateway route",
        },
    )

    by_id = {option.id: option for option in result.options}
    assert {"default", "sonnet", "opus", "haiku", "opusplan"} <= set(by_id)
    assert "env-model" in by_id
    assert "settings-model" not in by_id  # process env has higher precedence
    assert by_id["policy-a"].kind == "policy-allowed"
    assert by_id["opus-routed"].kind == "configured-model"
    assert by_id["custom-id"].display_name == "My routed model"
    assert by_id["custom-id"].description == "Internal gateway route"
    assert all(not option.entitlement_verified for option in result.options)


def test_claude_anthropic_api_paginates_and_scopes_entitlement(tmp_path: Path):
    calls: list[str | None] = []

    def fetcher(base_url, api_key, cursor):
        assert base_url == "https://api.anthropic.com"
        assert api_key == "secret-never-persisted"
        calls.append(cursor)
        if cursor is None:
            return {
                "data": [{"id": "claude-a", "display_name": "Claude A"}],
                "has_more": True,
                "last_id": "claude-a",
            }
        return {"data": [{"id": "claude-b"}], "has_more": False}

    result = discover_claude_models(
        home=tmp_path,
        env={},
        api_key="secret-never-persisted",
        fetcher=fetcher,
    )

    assert calls == [None, "claude-a"]
    api_options = [option for option in result.options if option.source == "anthropic-api"]
    assert [option.id for option in api_options] == ["claude-a", "claude-b"]
    assert all(option.entitlement_verified for option in api_options)
    assert "secret-never-persisted" not in result.model_dump_json()


def test_claude_gateway_requires_explicit_discovery_flag(tmp_path: Path):
    result = discover_claude_models(
        home=tmp_path,
        env={},
        api_key="secret",
        base_url="https://gateway.example.test",
    )

    assert result.available is True
    assert result.partial is True
    assert "not enabled" in (result.error or "")


def test_agy_parser_strips_controls_and_stable_dedupes():
    raw = "\x1b[32m- Model A\x1b[0m\nModel B\r\nModel A\n\x00Model C\n"
    assert parse_agy_models(raw) == ["Model A", "Model B", "Model C"]


def test_agy_discovery_distinguishes_empty_catalog_and_execution_error():
    empty = discover_agy_models(
        "/exact/agy", runner=lambda *_: CommandResult(returncode=0, stdout="\n")
    )
    failed = discover_agy_models(
        "/exact/agy", runner=lambda *_: CommandResult(returncode=1, stderr="not signed in")
    )

    assert empty.available is True
    assert empty.options == []
    assert empty.error is None
    assert failed.available is False
    assert "not signed in" in (failed.error or "")


def test_schema_capability_negative_cache_expires_and_refresh_bypasses(tmp_path: Path) -> None:
    """A transient probe failure must not disable model discovery for the whole process (§13.1).

    The old cache stored False on any failure forever. Now a negative result has a short TTL, the
    cached value is reused within it (no re-probe), and ``refresh`` bypasses it to re-check."""

    from openagent.runtimes.cli.model_discovery import (
        _CAPABILITY_NEGATIVE_TTL,
        _SCHEMA_CACHE,
        _schema_supports_model_list,
    )

    _SCHEMA_CACHE.clear()
    executable = tmp_path / "codex"
    executable.write_text("stub", encoding="utf-8")
    calls = {"n": 0}

    def flaky_runner(argv, timeout, limit):
        calls["n"] += 1
        if calls["n"] == 1:
            return CommandResult(returncode=1)  # transient nonzero exit
        out_dir = Path(argv[argv.index("--out") + 1])
        (out_dir / "schema.json").write_text('{"methods": ["model/list"]}', encoding="utf-8")
        return CommandResult(returncode=0)

    assert _schema_supports_model_list(str(executable), "v1", flaky_runner) is False
    key = (str(executable.resolve()), "v1")
    entry = _SCHEMA_CACHE[key]
    assert entry.supported is False
    # Short TTL, not the "forever" the old cache used. Recompute the sum rather than subtracting:
    # ``expires_at`` is stored as ``checked_at + ttl``, so ``checked_at + ttl`` reproduces the exact
    # float, whereas ``expires_at - checked_at`` of two large monotonic values is not exactly the ttl.
    assert entry.expires_at == entry.checked_at + _CAPABILITY_NEGATIVE_TTL

    # Within the TTL the negative is reused without a re-probe.
    assert _schema_supports_model_list(str(executable), "v1", flaky_runner) is False
    assert calls["n"] == 1

    # refresh forces a re-check, which now succeeds and is cached positive.
    assert _schema_supports_model_list(str(executable), "v1", flaky_runner, refresh=True) is True
    assert calls["n"] == 2
    assert _SCHEMA_CACHE[key].supported is True


def test_stderr_drain_keeps_a_bounded_tail() -> None:
    from openagent.runtimes.cli.model_discovery import _STDERR_TAIL_LIMIT, _drain_stderr

    async def run() -> tuple[bytes, bool]:
        reader = asyncio.StreamReader()
        reader.feed_data(b"A" * 20000)
        reader.feed_data(b"B" * 20000)
        reader.feed_eof()
        tail = bytearray()
        fired = {"overflow": False}
        await _drain_stderr(reader, tail, on_overflow=lambda: fired.__setitem__("overflow", True))
        return bytes(tail), fired["overflow"]

    tail, overflow = asyncio.run(run())
    assert overflow is False
    assert len(tail) <= _STDERR_TAIL_LIMIT  # bounded, not the full 40 KB
    assert tail.endswith(b"B")  # the *tail* is retained


def test_stderr_drain_terminates_a_runaway_stream() -> None:
    from openagent.runtimes.cli.model_discovery import (
        _STDERR_TAIL_LIMIT,
        MAX_PROTOCOL_BYTES,
        _drain_stderr,
    )

    async def run() -> tuple[bool, int]:
        reader = asyncio.StreamReader()
        reader.feed_data(b"x" * (MAX_PROTOCOL_BYTES + 4096))
        reader.feed_eof()
        tail = bytearray()
        fired = {"overflow": False}
        await _drain_stderr(reader, tail, on_overflow=lambda: fired.__setitem__("overflow", True))
        return fired["overflow"], len(tail)

    overflow, tail_len = asyncio.run(run())
    assert overflow is True  # the hard limit fired -> the runaway process is terminated
    assert tail_len <= _STDERR_TAIL_LIMIT
