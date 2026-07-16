"""Projected run state — the contract the Run Console renders from (item 3, item 22).

``events.jsonl`` is append-only, so "what is true now" has to be *projected* from it. These tests pin
the behaviours the old UI got wrong: a plan that grew a new card on every update, command output that
was re-appended in full each time the backend re-sent its aggregated buffer, and a reasoning summary
that was thrown away entirely.
"""

from __future__ import annotations

import json
from pathlib import Path

from openagent.core.events import EventType, ItemStatus, NormalizedEvent
from openagent.core.limits import RuntimeLimits
from openagent.core.projection import RunProjection
from openagent.runtimes.cli.codex import map_codex_event

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _project(fixture: str, workspace: Path | None = None) -> RunProjection:
    """Map a real Codex capture through the adapter and fold it into a projection."""

    projection = RunProjection("run_1")
    for line in (FIXTURES / fixture).read_text().splitlines():
        if not line.strip():
            continue
        for event in map_codex_event(json.loads(line), "run_1", workspace=workspace):
            projection.apply(event)
    return projection


def _event(etype: EventType, source: str = "codex-cli", **data) -> NormalizedEvent:
    return NormalizedEvent(run_id="run_1", type=etype, source=source, data=data)


# --------------------------------------------------------------------------- plan projection


def test_todo_updates_project_onto_one_plan_not_many():
    """started → updated ×N → completed is ONE checklist, not N+2 cards (item 22).

    Uses the real capture: Codex re-sends the whole ``todo_list`` under the same item id as it ticks
    items off. Keyed by (source, item_id), that projects onto a single plan whose entries flip to
    completed — which is what the console renders.
    """

    projection = _project("codex_v0142_plan.jsonl")

    plans = projection.by_kind("plan")
    assert len(plans) == 1, f"a re-sent plan created {len(plans)} cards"

    plan = projection.plan
    assert len(plan) == 4
    assert [step.completed for step in plan] == [True, True, True, True]
    assert plan[0].text == "Inspect top-level files and package metadata"


def test_plan_progress_is_visible_mid_run():
    projection = RunProjection("run_1")
    projection.apply(
        _event(
            EventType.PLAN_UPDATED,
            item_id="p1",
            status="in_progress",
            items=[
                {"text": "Inspect provider service", "completed": True},
                {"text": "Update run console", "completed": False},
                {"text": "Run tests", "completed": False},
            ],
        )
    )
    plan = projection.plan
    assert [p.completed for p in plan] == [True, False, False]
    assert projection.by_kind("plan")[0].title == "Plan (1/3)"


# --------------------------------------------------------------------------- command output


def test_aggregated_output_snapshots_replace_rather_than_accumulate():
    """Codex re-sends the whole buffer; the projection must not concatenate it repeatedly (item 22)."""

    projection = RunProjection("run_1")
    projection.apply(_event(EventType.COMMAND_STARTED, item_id="c1", command="pytest -q"))
    projection.apply(
        _event(EventType.COMMAND_OUTPUT, item_id="c1", output="line 1\n", snapshot=True)
    )
    projection.apply(
        _event(EventType.COMMAND_OUTPUT, item_id="c1", output="line 1\nline 2\n", snapshot=True)
    )
    projection.apply(
        _event(
            EventType.COMMAND_COMPLETED,
            item_id="c1",
            command="pytest -q",
            exit_code=0,
            output="line 1\nline 2\nline 3\n",
            snapshot=True,
        )
    )

    commands = projection.commands
    assert len(commands) == 1
    assert commands[0].output == "line 1\nline 2\nline 3\n"
    assert commands[0].output.count("line 1") == 1, "the aggregated buffer was appended repeatedly"
    assert commands[0].status == ItemStatus.COMPLETED.value


def test_incremental_chunks_still_append():
    projection = RunProjection("run_1")
    projection.apply(_event(EventType.COMMAND_STARTED, item_id="c1", command="build"))
    projection.apply(_event(EventType.COMMAND_OUTPUT, item_id="c1", output="a"))
    projection.apply(_event(EventType.COMMAND_OUTPUT, item_id="c1", output="b"))
    assert projection.commands[0].output == "ab"


def test_failed_command_is_not_rendered_as_success():
    projection = RunProjection("run_1")
    projection.apply(
        _event(
            EventType.COMMAND_COMPLETED,
            item_id="c1",
            command="pytest",
            exit_code=1,
            status=ItemStatus.FAILED.value,
        )
    )
    assert projection.commands[0].failed is True


# --------------------------------------------------------------------------- reasoning summary


def test_reasoning_summary_is_projected_with_its_text():
    projection = _project("codex_v0142_edit.jsonl")
    summaries = projection.reasoning
    assert summaries, "the reasoning summary was dropped"
    assert all(s.text.strip() for s in summaries), "a blank summary was projected"
    assert summaries[0].title == "Reasoning summary"


def test_reasoning_updates_replace_the_same_card():
    projection = RunProjection("run_1")
    projection.apply(
        _event(
            EventType.REASONING_SUMMARY,
            item_id="r1",
            status="in_progress",
            text="Looking at the parser",
        )
    )
    projection.apply(
        _event(
            EventType.REASONING_SUMMARY,
            item_id="r1",
            status="completed",
            text="Looked at the parser; it is fine",
        )
    )
    assert len(projection.reasoning) == 1
    assert projection.reasoning[0].text == "Looked at the parser; it is fine"
    assert projection.reasoning[0].status == "completed"


# --------------------------------------------------------------------------- files / usage / replay


def test_file_changes_keep_one_row_per_path():
    projection = _project("codex_v0142_edit.jsonl", workspace=Path("/workspace"))
    paths = [item.path for item in projection.files]
    assert "test_calc.py" in paths, f"expected a workspace-relative path, got {paths}"
    # The same file was added and later updated: two distinct file_change items, both addressable.
    assert all(not p.startswith("/") for p in paths), "an absolute path leaked into the projection"


def test_usage_accumulates_reasoning_tokens():
    projection = RunProjection("run_1")
    projection.apply(
        _event(EventType.USAGE_UPDATED, input_tokens=10, output_tokens=2, reasoning_tokens=5)
    )
    projection.apply(
        _event(EventType.USAGE_UPDATED, input_tokens=3, output_tokens=1, reasoning_tokens=4)
    )
    assert projection.usage["input_tokens"] == 13
    assert projection.usage["reasoning_tokens"] == 9


def test_replaying_the_same_events_rebuilds_the_same_state():
    """Reopening a run must reconstruct exactly what a live console had (item 10)."""

    events = []
    for line in (FIXTURES / "codex_v0142_edit.jsonl").read_text().splitlines():
        if line.strip():
            events.extend(map_codex_event(json.loads(line), "run_1"))

    live = RunProjection("run_1").apply_all(events)
    replayed = RunProjection("run_1").apply_all(events)

    assert [i.to_dict() for i in live.items] == [i.to_dict() for i in replayed.items]
    assert live.plan == replayed.plan
    assert live.final_message == replayed.final_message
    assert live.usage == replayed.usage


def test_items_from_different_sources_do_not_collide():
    """The key is (source, item_id): two backends reusing 'item_0' are two different things."""

    projection = RunProjection("run_1")
    projection.apply(
        _event(EventType.MESSAGE_COMPLETED, source="codex-cli", item_id="item_0", text="from codex")
    )
    projection.apply(
        _event(
            EventType.MESSAGE_COMPLETED,
            source="api-agent",
            item_id="item_0",
            text="from the api agent",
        )
    )
    assert len(projection.messages) == 2


# --------------------------------------------------------------------------- turns


def test_a_backend_that_restarts_item_ids_each_turn_does_not_overwrite_turn_1():
    """Codex numbers items from item_0 again on every turn — the turn must be part of the key.

    Found by a real resume: turn 2's answer ("84") arrived as `item_0`, exactly like turn 1's ("42").
    Keyed only by (source, item_id) the projection treated them as the *same* card, so turn 1's
    answer was silently overwritten and the console showed a single turn that had changed its mind.
    """

    projection = RunProjection("run_1")
    projection.apply(_event(EventType.MESSAGE_COMPLETED, item_id="item_0", text="42"))
    projection.apply(
        NormalizedEvent(
            run_id="run_1",
            type=EventType.SESSION_RESUMED,
            source="openagent",
            data={"turn": 2, "session_id": "th-1", "prompt": "double it"},
        )
    )
    projection.apply(_event(EventType.MESSAGE_COMPLETED, item_id="item_0", text="84"))

    messages = projection.messages
    assert [m.text for m in messages] == ["42", "84"]
    assert [m.turn for m in messages] == [1, 2]
    assert projection.final_message == "84"


def test_within_one_turn_the_same_item_id_still_projects_onto_one_card():
    projection = RunProjection("run_1")
    projection.apply(_event(EventType.COMMAND_STARTED, item_id="item_2", command="pytest"))
    projection.apply(
        _event(EventType.COMMAND_COMPLETED, item_id="item_2", command="pytest", exit_code=0)
    )
    assert len(projection.commands) == 1


def test_projection_byte_budget_evicts_oldest_and_marks_truncation(monkeypatch):
    import openagent.core.projection as projection_module

    monkeypatch.setattr(projection_module, "RUNTIME_LIMITS", RuntimeLimits(projection_bytes=700))
    projection = RunProjection("run_1")
    for index in range(8):
        projection.apply(
            _event(
                EventType.MESSAGE_COMPLETED,
                item_id=f"m{index}",
                text=f"{index}:" + ("x" * 180),
            )
        )

    assert projection.truncated is True
    assert len(projection.messages) < 8
    assert projection.messages[-1].text.startswith("7:")
