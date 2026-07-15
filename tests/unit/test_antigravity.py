"""Antigravity adapter mapping + registry (item 4).

The SUCCESS path is validated against a **recorded, real** fixture captured live from agy v1.1.0
(``tests/fixtures/antigravity_print.jsonl``). Failure/cancel mapping is checked with inline
synthetic objects — deliberately *not* recorded fixtures, since no real failure output was captured.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from openagent.runtimes.cli import antigravity as ag
from openagent.runtimes.cli.antigravity import (
    AntigravityAdapter,
    AntigravityPermissionError,
    map_antigravity_event,
)
from openagent.runtimes.cli.base import CliRunRequest
from openagent.runtimes.cli.registry import (
    build_cli_adapter,
    cli_display_name,
    cli_status_label,
    discover_cli_models,
    known_cli_types,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "antigravity_print.jsonl"


def _real_result() -> dict:
    line = FIXTURE.read_text(encoding="utf-8").strip().splitlines()[0]
    return json.loads(line)


def _types(events) -> list[str]:
    return [e.type if isinstance(e.type, str) else e.type.value for e in events]


# --------------------------------------------------------------------------- verified SUCCESS path

def test_maps_real_success_fixture():
    events = map_antigravity_event(_real_result(), "run_1")
    types = _types(events)
    assert "session.created" in types
    assert "message.completed" in types
    assert "usage.updated" in types
    assert types[-1] == "run.completed"

    session = next(e for e in events if _types([e]) == ["session.created"])
    assert session.data["provider_session_id"] == "938442f7-bca6-422b-b9d8-f8aa598de783"
    message = next(e for e in events if _types([e]) == ["message.completed"])
    assert message.data["text"] == "OK42\n"
    usage = next(e for e in events if _types([e]) == ["usage.updated"])
    assert usage.data["input_tokens"] == 39941
    assert usage.data["output_tokens"] == 7
    assert usage.data["provider_cost"] is None  # subscription product, no cost reported


def test_exactly_one_terminal_event_for_success():
    terminals = [t for t in _types(map_antigravity_event(_real_result(), "r"))
                 if t in ("run.completed", "run.failed", "run.cancelled")]
    assert terminals == ["run.completed"]


def test_thinking_tokens_normalized_to_reasoning_tokens():
    """Antigravity's ``thinking_tokens`` must surface as OpenAgent's ``reasoning_tokens`` (item 9).

    Every other backend (codex ``reasoning_output_tokens``, the API loop) reports reasoning under
    ``reasoning_tokens``; Antigravity would otherwise silently drop them from the usage schema.
    """

    obj = {
        "conversation_id": "c", "status": "SUCCESS", "response": "done",
        "usage": {"input_tokens": 10, "output_tokens": 5, "thinking_tokens": 42, "total_tokens": 57},
    }
    usage = next(e for e in map_antigravity_event(obj, "r") if _types([e]) == ["usage.updated"])
    assert usage.data["reasoning_tokens"] == 42
    assert usage.data["input_tokens"] == 10
    assert usage.data["output_tokens"] == 5
    # A missing thinking_tokens field normalizes to 0, never a KeyError.
    no_think = {"status": "SUCCESS", "response": "x", "usage": {"input_tokens": 1, "output_tokens": 1}}
    usage2 = next(e for e in map_antigravity_event(no_think, "r") if _types([e]) == ["usage.updated"])
    assert usage2.data["reasoning_tokens"] == 0


def test_capabilities_do_not_advertise_editing_as_safe_and_verified():
    """Editing is experimental/opt-in, so capabilities must not present it as normal & safe (item 9)."""

    default = asyncio.run(AntigravityAdapter(executable="agy", allow_experimental_edit=False).capabilities())
    assert default.experimental is True
    assert default.edits_files is False  # not enabled -> not advertised
    assert default.runs_commands is False
    assert default.resumable is True  # live-verified, still honestly reported

    opted_in = asyncio.run(
        AntigravityAdapter(executable="agy", allow_experimental_edit=True).capabilities()
    )
    assert opted_in.edits_files is True
    assert opted_in.experimental is True  # even opted-in, editing is experimental


# --------------------------------------------------------------------------- fail-closed statuses

def test_cancelled_status_maps_to_cancelled():
    events = map_antigravity_event({"conversation_id": "c", "status": "CANCELLED"}, "r")
    assert _types(events)[-1] == "run.cancelled"


def test_unknown_status_fails():
    events = map_antigravity_event({"conversation_id": "c", "status": "UNKNOWN"}, "r")
    assert _types(events)[-1] == "run.failed"


def test_aborted_status_fails():
    events = map_antigravity_event({"status": "ABORTED", "error": "fatal"}, "r")
    terminal = next(e for e in events if _types([e]) == ["run.failed"])
    assert terminal.data["message"] == "fatal"


def test_missing_status_fails():
    assert _types(map_antigravity_event({"conversation_id": "c"}, "r"))[-1] == "run.failed"


# --------------------------------------------------------------------------- adapter args + registry

def test_start_run_uses_print_json_flags(tmp_path: Path):
    adapter = AntigravityAdapter(executable="/usr/local/bin/agy", allow_experimental_edit=False)
    args = adapter._build_args(
        CliRunRequest(run_id="r", prompt="do it", workspace=tmp_path, permission_profile="read-only"),
        "do it",
    )
    assert args[:5] == ["/usr/local/bin/agy", "--print", "do it", "--output-format", "json"]
    assert "--model" not in args  # no model pinned → agy uses its own default


def test_pinned_model_is_passed_to_agy(tmp_path: Path):
    """A model discovered/pinned in the wizard must actually reach agy (--model), not be ignored."""

    adapter = AntigravityAdapter(executable="agy", allow_experimental_edit=False)
    args = adapter._build_args(
        CliRunRequest(run_id="r", prompt="x", workspace=tmp_path, permission_profile="read-only",
                      model="Gemini 3.5 Flash (Low)"),
        "x",
    )
    assert args[args.index("--model") + 1] == "Gemini 3.5 Flash (Low)"


# --------------------------------------------------------------------------- permission safety (item 15)


def test_safe_edit_never_implies_native_permission_bypass(tmp_path: Path):
    """``safe-edit`` must not silently disable Antigravity's own permission checks (item 15).

    Editing in ``--print`` mode is only possible via ``--dangerously-skip-permissions``, which turns
    Antigravity's tool checks off — and OpenAgent cannot observe Antigravity's internal tool calls to
    compensate. Calling that "safe-edit" would be a lie, so it is refused until the user opts in.
    """

    adapter = AntigravityAdapter(executable="/usr/local/bin/agy", allow_experimental_edit=False)
    allowed, reason = adapter.permission_status("safe-edit")
    assert allowed is False
    assert "EXPERIMENTAL" in reason

    with pytest.raises(AntigravityPermissionError):
        adapter._build_args(
            CliRunRequest(run_id="r", prompt="x", workspace=tmp_path,
                          permission_profile="safe-edit"),
            "x",
        )


def test_read_only_is_supported_by_default(tmp_path: Path):
    adapter = AntigravityAdapter(executable="agy", allow_experimental_edit=False)
    allowed, _ = adapter.permission_status("read-only")
    assert allowed is True
    args = adapter._build_args(
        CliRunRequest(run_id="r", prompt="x", workspace=tmp_path, permission_profile="read-only"),
        "x",
    )
    assert args[args.index("--mode") + 1] == "plan"


def test_experimental_opt_in_enables_editing(tmp_path: Path):
    adapter = AntigravityAdapter(executable="agy", allow_experimental_edit=True)
    allowed, reason = adapter.permission_status("safe-edit")
    assert allowed is True and "ENABLED" in reason
    args = adapter._build_args(
        CliRunRequest(run_id="r", prompt="x", workspace=tmp_path, permission_profile="safe-edit"),
        "x",
    )
    assert "--dangerously-skip-permissions" in args


def test_high_risk_profile_needs_its_own_opt_in(tmp_path: Path):
    """The experimental-edit opt-in is not enough for development/full-access — that needs its own."""

    adapter = AntigravityAdapter(executable="agy", allow_experimental_edit=True,
                                 allow_dangerous_bypass=False)
    allowed, reason = adapter.permission_status("full-access")
    assert allowed is False
    assert "DANGEROUS_BYPASS" in reason

    opted_in = AntigravityAdapter(executable="agy", allow_dangerous_bypass=True)
    allowed, _ = opted_in.permission_status("full-access")
    assert allowed is True


def test_permission_block_fails_the_run_rather_than_running_blind(tmp_path: Path):
    """A blocked profile produces one explicit run.failed — not a silent, edit-less "success"."""

    adapter = AntigravityAdapter(executable="/usr/local/bin/agy", allow_experimental_edit=False)

    async def _collect():
        return [
            e async for e in adapter.start_run(
                CliRunRequest(run_id="r", prompt="x", workspace=tmp_path,
                              permission_profile="safe-edit")
            )
        ]

    events = asyncio.run(_collect())
    assert _types(events) == ["run.failed"]
    assert events[0].data["error_type"] == "permission_mode_unsupported"


def test_resume_passes_conversation_id(tmp_path: Path):
    adapter = AntigravityAdapter(executable="agy")
    args = adapter._build_args(
        CliRunRequest(run_id="r", prompt="again", workspace=tmp_path, permission_profile="read-only"),
        "again", conversation="conv-123",
    )
    assert "--conversation" in args and "conv-123" in args
    assert args[args.index("--mode") + 1] == "plan"  # read-only -> plan mode, no edits


# --------------------------------------------------------------------------- model discovery (Phase 4)

def test_run_agy_models_parses_lines(monkeypatch):
    def _fake_run(args, **kw):
        return ag.subprocess.CompletedProcess(
            args, 0, stdout="Gemini 3.5 Flash (Low)\n\n  Claude Opus 4.6 (Thinking)  \n", stderr=""
        )

    monkeypatch.setattr(ag.subprocess, "run", _fake_run)
    assert ag._run_agy_models("/bin/agy") == [
        "Gemini 3.5 Flash (Low)", "Claude Opus 4.6 (Thinking)"
    ]


def test_run_agy_models_raises_on_nonzero_exit(monkeypatch):
    def _fake_run(args, **kw):
        return ag.subprocess.CompletedProcess(args, 1, stdout="", stderr="not signed in")

    monkeypatch.setattr(ag.subprocess, "run", _fake_run)
    with pytest.raises(RuntimeError, match="not signed in"):
        ag._run_agy_models("/bin/agy")


def test_discover_cli_models_success(monkeypatch):
    async def _models(self):
        return ["Model A", "Model B"]

    monkeypatch.setattr(AntigravityAdapter, "list_models", _models)
    result = asyncio.run(discover_cli_models("antigravity"))
    assert result.available is True
    assert result.models == ["Model A", "Model B"]
    assert result.method == "agy models"


def test_discover_cli_models_surfaces_the_real_error(monkeypatch):
    async def _boom(self):
        raise RuntimeError("`agy models` failed: not signed in")

    monkeypatch.setattr(AntigravityAdapter, "list_models", _boom)
    result = asyncio.run(discover_cli_models("antigravity"))
    assert result.available is False
    assert "not signed in" in (result.error or "")  # honest reason, not a silent empty list
    assert result.method == "agy models"


def test_discover_cli_models_unavailable_for_clis_without_listing():
    """Codex/Claude expose --model but no listing command — report that honestly, don't invent one."""

    for cli in ("codex", "claude"):
        result = asyncio.run(discover_cli_models(cli))
        assert result.available is False
        assert result.models == []
        assert "unavailable" in (result.error or "")


def test_registry_knows_antigravity():
    assert "antigravity" in known_cli_types()
    assert isinstance(build_cli_adapter("antigravity"), AntigravityAdapter)
    assert cli_display_name("antigravity") == "Antigravity"
    assert "Verified live" in cli_status_label("antigravity")


def test_not_installed_adapter_emits_single_failure(tmp_path: Path):
    adapter = AntigravityAdapter(executable="/nonexistent/agy")
    adapter.executable = None  # simulate "not installed" regardless of this host
    events = []

    async def _collect():
        async for e in adapter.start_run(
            CliRunRequest(run_id="r", prompt="x", workspace=tmp_path)
        ):
            events.append(e)

    import asyncio
    asyncio.run(_collect())
    assert _types(events) == ["run.failed"]
    assert events[0].data["error_type"] == "cli_not_found"


# --------------------------------------------------------------- end-to-end through a fake agy binary

@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shebang fake executable")
async def test_end_to_end_run_through_fake_agy(tmp_path: Path):
    """Drive the real adapter + run_managed_cli against a fake `agy` that prints the recorded result;
    exactly one terminal event (completed) must survive the finalizer."""
    fake = tmp_path / "agy"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        f"print(json.dumps({_real_result()!r}))\n",
        encoding="utf-8",
    )
    os.chmod(fake, 0o755)

    adapter = AntigravityAdapter(executable=str(fake))
    events = [
        e async for e in adapter.start_run(
            CliRunRequest(run_id="run_e2e", prompt="go", workspace=tmp_path,
                          permission_profile="read-only")
        )
    ]
    types = _types(events)
    assert types[0] == "process.started"
    terminals = [t for t in types if t in ("run.completed", "run.failed", "run.cancelled")]
    assert terminals == ["run.completed"]
    assert "session.created" in types and "usage.updated" in types
