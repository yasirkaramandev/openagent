"""Continuation artifact storage (spec §8.4).

The tests that matter most are the refusals. A store that writes happily and reads happily is easy;
what makes resume trustworthy is that a tampered, truncated, relocated or credential-bearing
artifact is refused rather than replayed, because a bad continuation does not produce an error at
the provider — it produces a worse turn that nobody can attribute weeks later.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from openagent.core.errors import ErrorType
from openagent.core.models import Protocol
from openagent.providers.continuation import ContinuationEnvelope, ContinuationStrategy
from openagent.providers.continuation_store import (
    HASH_NAME,
    METADATA_NAME,
    PAYLOAD_NAME,
    ContinuationRef,
    ContinuationStore,
    ContinuationStoreError,
)

pytestmark = pytest.mark.unit

SESSION = "sess_abc123"


@pytest.fixture
def store(tmp_path: Path) -> ContinuationStore:
    return ContinuationStore(tmp_path / "sessions")


def _envelope(**overrides) -> ContinuationEnvelope:
    kwargs = {
        "provider_type": "deepseek",
        "protocol": Protocol.OPENAI_CHAT,
        "strategy": ContinuationStrategy.NATIVE_MESSAGE_REPLAY,
        "native_assistant_message": {
            "role": "assistant",
            "content": "",
            "reasoning_content": "the model's thinking",
            "tool_calls": [{"id": "call_1", "function": {"name": "read", "arguments": "{}"}}],
        },
        "model_id": "deepseek-v4-pro",
    }
    kwargs.update(overrides)
    return ContinuationEnvelope.build(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- round trip


def test_save_then_load_returns_the_same_material(store: ContinuationStore):
    envelope = _envelope()
    ref = store.save(SESSION, envelope)

    loaded, warnings = store.load(
        ref, provider_type="deepseek", protocol=Protocol.OPENAI_CHAT, model_id="deepseek-v4-pro"
    )

    assert loaded.native_assistant_message == envelope.native_assistant_message
    assert loaded.content_hash == envelope.content_hash
    assert warnings == []


def test_save_writes_the_three_documented_files(store: ContinuationStore):
    store.save(SESSION, _envelope())
    directory = store.session_dir(SESSION)

    assert (directory / PAYLOAD_NAME).is_file()
    assert (directory / HASH_NAME).is_file()
    assert (directory / METADATA_NAME).is_file()


def test_the_reference_carries_only_row_sized_fields(store: ContinuationStore):
    ref = store.save(SESSION, _envelope())
    row = ref.as_row()

    assert set(row) == {
        "session_id",
        "artifact_path",
        "content_hash",
        "schema_version",
        "provider_type",
        "protocol",
        "resume_mode",
        "remote_interaction_id",
        "size_bytes",
    }
    # The payload never appears in what the database stores.
    assert "the model's thinking" not in json.dumps(row)
    assert row["resume_mode"] == "native_message_replay"


def test_a_remote_id_envelope_records_its_id_in_the_row(store: ContinuationStore):
    envelope = _envelope(
        provider_type="gemini",
        protocol=Protocol.GEMINI_INTERACTIONS,
        strategy=ContinuationStrategy.REMOTE_ID,
        remote_interaction_id="interactions/abc",
        native_assistant_message=None,
    )
    ref = store.save(SESSION, envelope)

    assert ref.remote_interaction_id == "interactions/abc"
    assert ref.as_row()["resume_mode"] == "remote_id"


def test_saving_twice_replaces_the_artifact(store: ContinuationStore):
    store.save(SESSION, _envelope())
    second = _envelope(native_assistant_message={"role": "assistant", "content": "later"})
    ref = store.save(SESSION, second)

    loaded, _ = store.load(ref, provider_type="deepseek", protocol=Protocol.OPENAI_CHAT)
    assert loaded.native_assistant_message == {"role": "assistant", "content": "later"}


# --------------------------------------------------------------------------- permissions


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_artifacts_are_owner_only(store: ContinuationStore):
    store.save(SESSION, _envelope())
    directory = store.session_dir(SESSION)

    assert directory.stat().st_mode & 0o777 == 0o700
    for name in (PAYLOAD_NAME, HASH_NAME, METADATA_NAME):
        assert (directory / name).stat().st_mode & 0o777 == 0o600


# --------------------------------------------------------------------------- integrity


def test_a_tampered_payload_is_refused(store: ContinuationStore):
    ref = store.save(SESSION, _envelope())
    payload = store.payload_path(SESSION)
    doctored = json.loads(payload.read_text())
    doctored["native_assistant_message"]["content"] = "injected"
    payload.write_text(json.dumps(doctored))

    with pytest.raises(ContinuationStoreError) as exc:
        store.load(ref, provider_type="deepseek", protocol=Protocol.OPENAI_CHAT)

    assert exc.value.error_type is ErrorType.CONTINUATION_INVALID


def test_a_payload_whose_hash_file_disagrees_is_refused(store: ContinuationStore):
    ref = store.save(SESSION, _envelope())
    (store.session_dir(SESSION) / HASH_NAME).write_text("0" * 64 + "\n")

    with pytest.raises(ContinuationStoreError) as exc:
        store.load(ref, provider_type="deepseek", protocol=Protocol.OPENAI_CHAT)

    assert "hash file" in exc.value.message


def test_a_row_pointing_at_a_different_artifact_is_refused(store: ContinuationStore):
    # Resuming the wrong conversation is worse than failing to resume.
    ref = store.save(SESSION, _envelope())
    wrong = ContinuationRef(**{**ref.__dict__, "content_hash": "f" * 64})

    with pytest.raises(ContinuationStoreError) as exc:
        store.load(wrong, provider_type="deepseek", protocol=Protocol.OPENAI_CHAT)

    assert "does not match the hash recorded" in exc.value.message


def test_a_missing_artifact_is_a_typed_error(store: ContinuationStore):
    ref = store.save(SESSION, _envelope())
    store.payload_path(SESSION).unlink()

    with pytest.raises(ContinuationStoreError) as exc:
        store.load(ref, provider_type="deepseek", protocol=Protocol.OPENAI_CHAT)

    assert "missing" in exc.value.message


def test_an_unparseable_artifact_is_a_typed_error(store: ContinuationStore):
    ref = store.save(SESSION, _envelope())
    store.payload_path(SESSION).write_text("{not json")

    with pytest.raises(ContinuationStoreError) as exc:
        store.load(ref, provider_type="deepseek", protocol=Protocol.OPENAI_CHAT)

    assert "not a readable envelope" in exc.value.message


def test_an_oversized_artifact_is_refused_before_it_is_parsed(store: ContinuationStore):
    from openagent.providers.continuation_store import MAX_PAYLOAD_BYTES

    ref = store.save(SESSION, _envelope())
    store.payload_path(SESSION).write_text("x" * (MAX_PAYLOAD_BYTES + 1))

    with pytest.raises(ContinuationStoreError) as exc:
        store.load(ref, provider_type="deepseek", protocol=Protocol.OPENAI_CHAT)

    assert "ceiling" in exc.value.message


# --------------------------------------------------------------------------- binding


def test_replaying_into_a_different_provider_is_refused(store: ContinuationStore):
    ref = store.save(SESSION, _envelope())

    with pytest.raises(ContinuationStoreError) as exc:
        store.load(ref, provider_type="minimax", protocol=Protocol.OPENAI_CHAT)

    assert "deepseek" in exc.value.message


def test_replaying_over_a_different_protocol_is_refused(store: ContinuationStore):
    ref = store.save(SESSION, _envelope())

    with pytest.raises(ContinuationStoreError):
        store.load(ref, provider_type="deepseek", protocol=Protocol.ANTHROPIC_MESSAGES)


def test_a_changed_model_warns_rather_than_refuses(store: ContinuationStore):
    # Half-working is the user's call to make, so it is surfaced, not decided here.
    ref = store.save(SESSION, _envelope())

    _loaded, warnings = store.load(
        ref, provider_type="deepseek", protocol=Protocol.OPENAI_CHAT, model_id="deepseek-v4-flash"
    )

    assert any("deepseek-v4-pro" in w for w in warnings)


# --------------------------------------------------------------------------- symlink safety


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_a_symlinked_payload_is_refused_on_write(store: ContinuationStore, tmp_path: Path):
    # os.replace onto a symlink would write through it to somewhere else entirely.
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched")
    directory = store.session_dir(SESSION)
    directory.mkdir(parents=True)
    (directory / PAYLOAD_NAME).symlink_to(outside)

    with pytest.raises(ContinuationStoreError) as exc:
        store.save(SESSION, _envelope())

    assert "symbolic link" in exc.value.message
    assert outside.read_text() == "untouched"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_a_symlinked_session_directory_is_refused(store: ContinuationStore, tmp_path: Path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    store.session_dir(SESSION).parent.mkdir(parents=True, exist_ok=True)
    store.session_dir(SESSION).symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(ContinuationStoreError):
        store.save(SESSION, _envelope())


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_a_symlinked_payload_is_refused_on_read(store: ContinuationStore, tmp_path: Path):
    ref = store.save(SESSION, _envelope())
    payload = store.payload_path(SESSION)
    payload.unlink()
    payload.symlink_to(tmp_path / "somewhere-else.json")

    with pytest.raises(ContinuationStoreError):
        store.load(ref, provider_type="deepseek", protocol=Protocol.OPENAI_CHAT)


# --------------------------------------------------------------------------- session ids


@pytest.mark.parametrize(
    "session_id",
    ["../escape", "a/b", "..", "", "with space", "sess\x00null", "x" * 129, "dots.are.excluded"],
)
def test_unsafe_session_ids_are_refused(store: ContinuationStore, session_id: str):
    with pytest.raises(ContinuationStoreError):
        store.save(session_id, _envelope())


@pytest.mark.parametrize("session_id", ["abc123", "sess_1", "run-42", "a" * 128])
def test_safe_session_ids_are_accepted(store: ContinuationStore, session_id: str):
    assert store.save(session_id, _envelope()).session_id == session_id


# --------------------------------------------------------------------------- secrets


def test_material_carrying_a_key_shaped_token_is_refused(store: ContinuationStore):
    # The payload cannot be redacted without corrupting replay, so it is not written at all.
    envelope = _envelope(
        native_assistant_message={"role": "assistant", "content": "use sk-live-abcdef123456"}
    )

    with pytest.raises(ContinuationStoreError) as exc:
        store.save(SESSION, envelope)

    assert "key-shaped token" in exc.value.message
    assert not store.payload_path(SESSION).exists()


def test_the_refusal_message_does_not_repeat_the_secret(store: ContinuationStore):
    envelope = _envelope(opaque_fields={"note": "Bearer abcdef1234567890"})

    with pytest.raises(ContinuationStoreError) as exc:
        store.save(SESSION, envelope)

    assert "abcdef1234567890" not in exc.value.message


# --------------------------------------------------------------------------- metadata


def test_metadata_describes_the_artifact_without_opening_it(store: ContinuationStore):
    store.save(SESSION, _envelope())
    described = store.describe(SESSION)

    assert described is not None
    assert described["provider_type"] == "deepseek"
    assert described["strategy"] == "native_message_replay"
    assert described["session_id"] == SESSION
    # No reasoning, no native message, no tool arguments.
    assert "the model's thinking" not in json.dumps(described)
    assert "native_assistant_message" not in described


def test_describing_an_unknown_session_returns_none(store: ContinuationStore):
    assert store.describe("nothing_here") is None


# --------------------------------------------------------------------------- delete


def test_delete_removes_the_artifacts(store: ContinuationStore):
    store.save(SESSION, _envelope())
    store.delete(SESSION)

    assert not store.session_dir(SESSION).exists()


def test_delete_is_idempotent(store: ContinuationStore):
    store.delete(SESSION)
    store.delete(SESSION)


def test_delete_leaves_unrelated_files_alone(store: ContinuationStore):
    store.save(SESSION, _envelope())
    stray = store.session_dir(SESSION) / "notes.txt"
    stray.write_text("keep me")

    store.delete(SESSION)

    assert stray.read_text() == "keep me"
