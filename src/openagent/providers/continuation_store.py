"""On-disk storage for provider-native continuation material (spec §8.4).

A continuation envelope can be a quarter of a megabyte of provider-native JSON — reasoning blocks,
tool-call structures, native step lists. Putting that in a database column works right up until it
doesn't: every query that touches the row pays for it, every backup carries it, every migration
rewrites it, and the row that was supposed to make resume cheap becomes the reason listing sessions
is slow.

So the payload is a file and the database keeps a reference: where it is, what it hashes to, which
schema wrote it, and which provider/protocol/resume-mode it is bound to. Those are the fields a
query actually filters on, and none of them is large.

Three properties the file layout has to carry, because nothing downstream can add them later:

* **It is the user's data at rest.** ``0600`` files in a ``0700`` directory, written atomically so a
  crash mid-write leaves the previous artifact rather than a truncated one.
* **It is verifiable.** The hash is stored in the envelope *and* beside it. A payload that does not
  match its hash is refused rather than replayed — a truncated native message does not error at the
  provider, it degrades the turn, which is exactly the failure mode that is impossible to attribute
  later.
* **It is not a place secrets end up.** The payload is written verbatim because replay requires
  fidelity, so instead of redacting it (which would corrupt it) a payload containing key-shaped
  tokens is *refused*. Normalized-history replay is always available as a fallback, which makes
  fail-closed the cheap option here.

``metadata.json`` exists so that Doctor and the session list can describe an artifact — provider,
size, age, resume mode — without opening the payload at all.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.errors import ErrorType, OpenAgentError, redact_secrets
from ..core.models import Protocol, utcnow
from ..security.atomic import atomic_write_bytes
from .continuation import (
    MAX_ENVELOPE_BYTES,
    ContinuationEnvelope,
    ContinuationStrategy,
)

PAYLOAD_NAME = "continuation.json"
HASH_NAME = "continuation.sha256"
METADATA_NAME = "metadata.json"

#: Session ids become directory names. Dots are excluded outright rather than filtered, so no
#: combination of them can climb out of the sessions directory.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

#: A stored payload may have been replaced since it was written. Reading is bounded by the same
#: ceiling writing is, so a swapped-in oversized file is refused before it is parsed.
MAX_PAYLOAD_BYTES = MAX_ENVELOPE_BYTES + 8192

_DIR_MODE = 0o700
_FILE_MODE = 0o600


class ContinuationStoreError(OpenAgentError):
    """An artifact could not be written, or cannot be trusted to be read."""

    def __init__(self, message: str) -> None:
        super().__init__(ErrorType.CONTINUATION_INVALID, redact_secrets(message))


@dataclass(frozen=True)
class ContinuationRef:
    """What the database row holds — everything except the payload (spec §8.4)."""

    session_id: str
    artifact_path: str
    content_hash: str
    schema_version: int
    provider_type: str
    protocol: Protocol
    #: The resume mode this material supports.
    strategy: ContinuationStrategy
    remote_interaction_id: str | None = None
    size_bytes: int = 0

    def as_row(self) -> dict[str, Any]:
        """The reference in the shape a storage layer persists."""

        return {
            "session_id": self.session_id,
            "artifact_path": self.artifact_path,
            "content_hash": self.content_hash,
            "schema_version": self.schema_version,
            "provider_type": self.provider_type,
            "protocol": self.protocol.value,
            "resume_mode": self.strategy.value,
            "remote_interaction_id": self.remote_interaction_id,
            "size_bytes": self.size_bytes,
        }


class ContinuationStore:
    """Reads and writes continuation artifacts under one sessions directory."""

    def __init__(self, sessions_dir: Path) -> None:
        self._root = sessions_dir

    # ------------------------------------------------------------------ paths

    def session_dir(self, session_id: str) -> Path:
        return self._root / _validated_session_id(session_id)

    def payload_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / PAYLOAD_NAME

    # ------------------------------------------------------------------ write

    def save(self, session_id: str, envelope: ContinuationEnvelope) -> ContinuationRef:
        """Persist ``envelope`` for ``session_id`` and return the row to store.

        Refuses rather than truncates or redacts. Every rejection here leaves the caller able to
        fall back to normalized-history replay, which is why fail-closed is affordable.
        """

        directory = self.session_dir(session_id)
        payload = envelope.model_dump_json().encode("utf-8")

        if len(payload) > MAX_PAYLOAD_BYTES:
            raise ContinuationStoreError(
                f"continuation artifact is {len(payload)} bytes, over the "
                f"{MAX_PAYLOAD_BYTES}-byte ceiling"
            )
        _refuse_if_secret_shaped(envelope)

        _prepare_directory(directory)
        for name in (PAYLOAD_NAME, HASH_NAME, METADATA_NAME):
            _refuse_symlink(directory / name)

        metadata = {
            **envelope.redacted(),
            "session_id": session_id,
            "written_at": utcnow().isoformat(),
        }

        atomic_write_bytes(directory / PAYLOAD_NAME, payload, mode=_FILE_MODE)
        atomic_write_bytes(
            directory / HASH_NAME, f"{envelope.content_hash}\n".encode(), mode=_FILE_MODE
        )
        atomic_write_bytes(
            directory / METADATA_NAME,
            json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8"),
            mode=_FILE_MODE,
        )

        return ContinuationRef(
            session_id=session_id,
            artifact_path=str(directory / PAYLOAD_NAME),
            content_hash=envelope.content_hash,
            schema_version=envelope.schema_version,
            provider_type=envelope.provider_type,
            protocol=envelope.protocol,
            strategy=envelope.strategy,
            remote_interaction_id=envelope.remote_interaction_id,
            size_bytes=envelope.size_bytes,
        )

    # ------------------------------------------------------------------ read

    def load(
        self,
        ref: ContinuationRef,
        *,
        provider_type: str,
        protocol: Protocol,
        model_id: str | None = None,
    ) -> tuple[ContinuationEnvelope, list[str]]:
        """Load and fully verify the artifact ``ref`` points at.

        Verification is not optional and not deferred. Returns the envelope plus any non-fatal
        warnings (a model change is the user's call); anything that would produce a malformed or
        untrustworthy request raises.
        """

        path = self.payload_path(ref.session_id)
        _refuse_symlink(path)
        try:
            size = path.stat().st_size
        except FileNotFoundError as exc:
            raise ContinuationStoreError(
                f"continuation artifact for session {ref.session_id} is missing"
            ) from exc
        except OSError as exc:
            raise ContinuationStoreError(
                f"continuation artifact for session {ref.session_id} could not be read"
            ) from exc
        if size > MAX_PAYLOAD_BYTES:
            raise ContinuationStoreError(
                f"continuation artifact for session {ref.session_id} is {size} bytes, over the "
                f"{MAX_PAYLOAD_BYTES}-byte ceiling"
            )

        try:
            envelope = ContinuationEnvelope.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise ContinuationStoreError(
                f"continuation artifact for session {ref.session_id} is not a readable envelope"
            ) from exc

        # The row and the file must agree before the file's own claims are trusted: a row pointing
        # at a different artifact is a bug that would otherwise resume the wrong conversation.
        if envelope.content_hash != ref.content_hash:
            raise ContinuationStoreError(
                f"continuation artifact for session {ref.session_id} does not match the hash "
                f"recorded for it"
            )
        sidecar = self._read_sidecar_hash(ref.session_id)
        if sidecar is not None and sidecar != envelope.content_hash:
            raise ContinuationStoreError(
                f"continuation artifact for session {ref.session_id} does not match its hash file"
            )

        try:
            warnings = envelope.verify(
                provider_type=provider_type, protocol=protocol, model_id=model_id
            )
        except Exception as exc:  # ContinuationError and anything verify() may raise
            raise ContinuationStoreError(str(exc)) from exc
        return envelope, warnings

    def describe(self, session_id: str) -> dict[str, Any] | None:
        """The artifact's metadata, for Doctor and session listings.

        Reads ``metadata.json`` and never the payload, so describing a session cannot surface
        reasoning or native provider material (spec §8.4, §24).
        """

        path = self.session_dir(session_id) / METADATA_NAME
        try:
            _refuse_symlink(path)
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, ContinuationStoreError):
            return None
        return data if isinstance(data, dict) else None

    def delete(self, session_id: str) -> None:
        """Remove a session's artifacts. Idempotent; leaves unrelated files alone."""

        directory = self.session_dir(session_id)
        for name in (PAYLOAD_NAME, HASH_NAME, METADATA_NAME):
            target = directory / name
            try:
                if target.is_symlink() or target.exists():
                    target.unlink()
            except OSError:
                continue
        try:
            directory.rmdir()
        except OSError:
            # Not empty (something else lives here) or already gone; either is fine.
            pass

    def _read_sidecar_hash(self, session_id: str) -> str | None:
        path = self.session_dir(session_id) / HASH_NAME
        try:
            _refuse_symlink(path)
            return path.read_text(encoding="utf-8").strip() or None
        except (OSError, ContinuationStoreError):
            return None


# ------------------------------------------------------------------------------- helpers


def _validated_session_id(session_id: str) -> str:
    if not _SESSION_ID_RE.fullmatch(session_id or ""):
        raise ContinuationStoreError(
            "session id is not a safe directory name (letters, digits, '_' and '-' only)"
        )
    return session_id


def _prepare_directory(directory: Path) -> None:
    _refuse_symlink(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    try:
        directory.chmod(_DIR_MODE)
    except OSError:  # pragma: no cover - filesystem without POSIX modes
        pass


def _refuse_symlink(path: Path) -> None:
    """Refuse to follow a symlink at ``path``.

    ``atomic_write_bytes`` ends in ``os.replace``, which would happily replace a symlink's *target*
    somewhere else on the filesystem. Checking with ``lstat`` — never ``exists()``, which follows —
    keeps a planted link from turning a continuation write into an arbitrary-file write.
    """

    try:
        if path.is_symlink():
            raise ContinuationStoreError(f"refusing to use {path.name}: it is a symbolic link")
    except OSError:  # pragma: no cover - unreadable parent
        return


def _refuse_if_secret_shaped(envelope: ContinuationEnvelope) -> None:
    """Refuse to persist native material that looks like it carries a credential.

    The payload cannot be redacted — replay needs it byte-exact — so the only way to guarantee a
    key never lands in the artifact is not to write it. A provider echoing a credential into an
    assistant message is a provider bug; storing it to disk on their behalf would make it ours.
    """

    material = json.dumps(
        {
            "native_assistant_message": envelope.native_assistant_message,
            "native_steps": envelope.native_steps,
            "opaque_fields": envelope.opaque_fields,
        },
        sort_keys=True,
    )
    if redact_secrets(material) != material:
        raise ContinuationStoreError(
            "continuation material contains a key-shaped token; refusing to write it to disk. "
            "Resume falls back to normalized history."
        )
