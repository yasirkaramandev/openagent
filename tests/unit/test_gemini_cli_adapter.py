"""Gemini CLI adapter (spec §11).

Most of these assert a *refusal to claim* something: that resume is unsupported, that live
streaming is not reported, that JSON output is probed rather than assumed. Those are the
assertions that would silently invert if someone later decided the documentation was good enough.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openagent.core.errors import ErrorType
from openagent.core.events import EventType
from openagent.runtimes.cli.base import CliRunRequest
from openagent.runtimes.cli.gemini import (
    APPROVAL_AUTO_EDIT,
    APPROVAL_DEFAULT,
    APPROVAL_YOLO,
    RESUME_SPIKE_REQUIRED,
    GeminiCliAdapter,
    build_command,
    map_result,
    permission_mapping,
    probe_json_output_support,
)

pytestmark = pytest.mark.unit


def _request(**kwargs) -> CliRunRequest:
    defaults = {
        "run_id": "run_1",
        "prompt": "do the thing",
        "workspace": Path("/tmp/ws"),
        "permission_profile": "safe-edit",
    }
    defaults.update(kwargs)
    return CliRunRequest(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- permissions


def test_safe_edit_uses_auto_edit_approval_inside_the_sandbox():
    mapping = permission_mapping("safe-edit")

    assert mapping.approval_mode == APPROVAL_AUTO_EDIT
    assert mapping.sandbox


def test_read_only_withholds_the_mutating_tools_rather_than_just_prompting():
    # An approval prompt is something a model can talk its way past; withholding the tool is not.
    mapping = permission_mapping("read-only")

    assert mapping.approval_mode == APPROVAL_DEFAULT
    assert set(mapping.denied_tools) == {"run_shell_command", "write_file", "replace"}


def test_full_still_sandboxes():
    assert permission_mapping("full").sandbox


def test_plan_is_read_only_plus_an_instruction():
    mapping = permission_mapping("plan")

    assert mapping.denied_tools
    assert "plan only" in mapping.prompt_prefix.lower()


def test_yolo_is_not_offered_as_a_profile():
    # The CLI has it; OpenAgent does not surface it, because a profile in a list is a profile
    # someone picks without reading what it does.
    assert APPROVAL_YOLO not in {m.approval_mode for m in _all_mappings()}


def test_an_unknown_profile_falls_back_to_the_most_restrictive_mapping():
    # A default should fail toward less capability, not more.
    assert permission_mapping("something-new") == permission_mapping("read-only")


def _all_mappings():
    return [permission_mapping(name) for name in ("read-only", "safe-edit", "full", "plan")]


def test_every_offered_profile_sandboxes():
    assert all(mapping.sandbox for mapping in _all_mappings())


# --------------------------------------------------------------------------- command


def test_the_command_uses_the_documented_headless_flags():
    argv = build_command(_request(), json_output=True)

    assert argv[0] == "gemini"
    assert "--prompt" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--approval-mode") + 1] == APPROVAL_AUTO_EDIT
    assert "--sandbox" in argv


def test_the_json_flag_is_omitted_when_the_binary_does_not_have_it():
    # Passing it to a build that lacks it fails the run with an argument error.
    argv = build_command(_request(), json_output=False)

    assert "--output-format" not in argv


def test_the_model_is_pinned_when_the_agent_specifies_one():
    argv = build_command(_request(model="gemini-3.6-flash"), json_output=True)

    assert argv[argv.index("--model") + 1] == "gemini-3.6-flash"


def test_no_model_flag_is_sent_when_the_agent_has_no_preference():
    assert "--model" not in build_command(_request(), json_output=True)


def test_the_plan_prefix_is_prepended_to_the_prompt():
    argv = build_command(_request(permission_profile="plan"), json_output=True)
    prompt = argv[argv.index("--prompt") + 1]

    assert prompt.endswith("do the thing")
    assert "plan only" in prompt.lower()


def test_an_explicit_executable_is_honoured():
    assert (
        build_command(_request(), executable="/opt/gemini", json_output=False)[0] == "/opt/gemini"
    )


# --------------------------------------------------------------------------- json probe


async def test_json_support_is_read_from_the_binarys_own_help():
    async def fake_help(_executable: str) -> str:
        return "Options:\n  --output-format  Output format\n"

    support = await probe_json_output_support("gemini", runner=fake_help)

    assert support.supported
    assert support.structured_events


async def test_a_build_without_the_flag_is_reported_unsupported_not_broken():
    # gemini-cli#9009: released versions answer "Unknown arguments: output-format".
    async def fake_help(_executable: str) -> str:
        return "Options:\n  --prompt  Run headlessly\n"

    support = await probe_json_output_support("gemini", runner=fake_help)

    assert not support.supported
    assert "does not advertise" in support.detail


async def test_a_failed_probe_answers_no_rather_than_yes():
    async def boom(_executable: str) -> str:
        raise OSError("no such file")

    support = await probe_json_output_support("gemini", runner=boom)

    assert not support.supported
    assert "could not read" in support.detail


async def test_capabilities_follow_the_probe(monkeypatch):
    adapter = GeminiCliAdapter(executable="gemini")

    async def fake_help(_executable: str) -> str:
        return "--output-format"

    monkeypatch.setattr("openagent.runtimes.cli.gemini._run_help", fake_help)
    capabilities = await adapter.capabilities()

    assert capabilities.structured_events


async def test_capabilities_report_no_structured_events_when_the_flag_is_absent(monkeypatch):
    adapter = GeminiCliAdapter(executable="gemini")

    async def fake_help(_executable: str) -> str:
        return "no such flag here"

    monkeypatch.setattr("openagent.runtimes.cli.gemini._run_help", fake_help)
    capabilities = await adapter.capabilities()

    assert not capabilities.structured_events


# --------------------------------------------------------------------------- honesty


async def test_resume_is_not_claimed(monkeypatch):
    async def fake_help(_executable: str) -> str:
        return "--output-format"

    monkeypatch.setattr("openagent.runtimes.cli.gemini._run_help", fake_help)
    capabilities = await GeminiCliAdapter(executable="gemini").capabilities()

    # Claiming it would make the wizard offer a resume that silently starts a new conversation.
    assert not capabilities.resumable


def test_resuming_explains_what_would_have_to_be_observed_first():
    adapter = GeminiCliAdapter(executable="gemini")

    with pytest.raises(NotImplementedError) as exc:
        adapter.resume_run("sess", "prompt", _request())

    assert RESUME_SPIKE_REQUIRED in str(exc.value)
    assert "not native resume" in str(exc.value)


def test_live_streaming_is_reported_as_absent():
    # Headless gemini returns one object at the end; separating the two answers is the point.
    assert GeminiCliAdapter(executable="gemini").live_structured_events is False


async def test_the_adapter_is_marked_experimental(monkeypatch):
    async def fake_help(_executable: str) -> str:
        return "--output-format"

    monkeypatch.setattr("openagent.runtimes.cli.gemini._run_help", fake_help)

    assert (await GeminiCliAdapter(executable="gemini").capabilities()).experimental


# --------------------------------------------------------------------------- result mapping


def test_a_successful_result_yields_a_message_and_one_terminal():
    events = map_result({"response": "all done"}, "run_1")

    types = [e.type for e in events]
    assert types == [EventType.MESSAGE_COMPLETED.value, EventType.RUN_COMPLETED.value]
    assert events[0].data["text"] == "all done"


def test_no_message_delta_is_ever_synthesized():
    # A fabricated delta is indistinguishable downstream from one that really streamed.
    events = map_result({"response": "line one\nline two\nline three"}, "run_1")

    assert EventType.MESSAGE_DELTA.value not in [e.type for e in events]


def test_exactly_one_terminal_event_is_emitted():
    events = map_result({"response": "x", "stats": {"models": {}}}, "run_1")
    terminals = [
        e
        for e in events
        if e.type
        in {
            EventType.RUN_COMPLETED.value,
            EventType.RUN_FAILED.value,
            EventType.RUN_CANCELLED.value,
        }
    ]

    assert len(terminals) == 1


def test_an_error_result_fails_the_run_and_emits_no_message():
    events = map_result(
        {"error": {"type": "AuthError", "message": "not signed in", "code": 401}}, "run_1"
    )

    assert [e.type for e in events] == [EventType.RUN_FAILED.value]
    assert events[0].data["error_type"] == ErrorType.AUTHENTICATION_FAILED.value
    assert "not signed in" in events[0].data["message"]


def test_a_rate_limited_error_is_classified_from_its_code():
    events = map_result({"error": {"message": "slow down", "code": 429}}, "run_1")

    assert events[0].data["error_type"] == ErrorType.PROVIDER_RATE_LIMITED.value


def test_an_error_without_a_code_is_classified_from_its_text():
    events = map_result({"error": {"type": "QuotaExceeded", "message": "quota gone"}}, "run_1")

    assert events[0].data["error_type"] == ErrorType.PROVIDER_RATE_LIMITED.value


def test_an_unrecognisable_error_is_a_command_failure_not_a_guess():
    events = map_result({"error": {"message": "something odd"}}, "run_1")

    assert events[0].data["error_type"] == ErrorType.COMMAND_FAILED.value


def test_an_empty_error_object_is_not_treated_as_a_failure():
    events = map_result({"response": "fine", "error": {}}, "run_1")

    assert EventType.RUN_COMPLETED.value in [e.type for e in events]


def test_token_stats_are_summed_across_models():
    payload = {
        "response": "x",
        "stats": {
            "models": {
                "gemini-a": {"tokens": {"prompt": 10, "candidates": 5}},
                "gemini-b": {"tokens": {"prompt": 3}},
            }
        },
    }
    events = map_result(payload, "run_1")
    usage = [e for e in events if e.type == EventType.USAGE_UPDATED.value]

    assert usage[0].data["tokens"] == {"prompt": 13, "candidates": 5}


def test_unrecognisable_stats_are_left_alone_rather_than_coerced():
    events = map_result({"response": "x", "stats": {"models": "not a mapping"}}, "run_1")

    assert EventType.USAGE_UPDATED.value not in [e.type for e in events]


def test_non_integer_token_values_are_ignored():
    payload = {"response": "x", "stats": {"models": {"m": {"tokens": {"prompt": "many"}}}}}
    events = map_result(payload, "run_1")

    assert EventType.USAGE_UPDATED.value not in [e.type for e in events]


def test_a_result_with_no_response_still_terminates_the_run():
    events = map_result({}, "run_1")

    assert [e.type for e in events] == [EventType.RUN_COMPLETED.value]


def test_every_event_is_attributed_to_the_gemini_cli():
    events = map_result({"response": "x"}, "run_1")

    assert all(e.source == "gemini-cli" for e in events)
    assert all(e.run_id == "run_1" for e in events)


# --------------------------------------------------------------------------- auth


async def test_an_api_key_in_the_environment_is_detected_by_name_only(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-value")
    status = await GeminiCliAdapter(executable="gemini").inspect_auth()

    assert status.authenticated
    assert status.environment_names == ["GEMINI_API_KEY"]
    # The value must never travel with the status.
    assert "secret-value" not in status.detail
    assert "secret-value" not in str(status.environment_names)


async def test_vertex_credentials_are_recognised(monkeypatch):
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")

    status = await GeminiCliAdapter(executable="gemini").inspect_auth()

    assert status.authenticated
    assert "Vertex" in status.detail


async def test_an_undetectable_google_login_does_not_block_the_run(monkeypatch):
    # Absence of an env var is not proof of anything; the CLI's own error beats our guess.
    for name in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_LOCATION",
    ):
        monkeypatch.delenv(name, raising=False)

    status = await GeminiCliAdapter(executable="gemini").inspect_auth()

    assert not status.authenticated
    assert not status.blocking
