"""What happens when a CLI update is available, per policy.

``CliUpdatePolicy.ASK`` did not ask. ``preflight.py`` handled ``NEVER`` and ``AUTO`` and then fell
through to a single warning line for everything else, so ``ASK`` — the **default** policy — behaved
identically to ``NOTIFY``: print a message, start the run anyway. A user who selected "ask me" was
never asked, and had no way to notice that.

The fix needs care in two places, because the naive version of each is worse than doing nothing:

* **A prompt that cannot be answered must not block.** OpenAgent runs from cron, from CI, and from
  a piped shell. An ``ASK`` that waits for input in those contexts hangs the run forever. With no
  callback wired up, ASK degrades to NOTIFY rather than to a stall — the same default-safe shape as
  :class:`~openagent.security.approvals.ApprovalGate`, which denies rather than waits.

* **"Don't ask again" must be scoped to what the user actually saw.** Suppressing by CLI name alone
  would silence the prompt for every future version, including a security update. The key includes
  the exact version pair *and* a fingerprint of the active executable, so the answer expires the
  moment any of those change — which is precisely when the question becomes new again.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from ...core.models import CliInstallation, CliUpdateStatus
from ...security.atomic import atomic_write_text
from ...security.file_lock import LockTimeout, file_lock


class UpdateChoice(str, Enum):
    """What the user decided when asked about a pending CLI update."""

    UPDATE_NOW = "update-now"
    CONTINUE_WITHOUT_UPDATING = "continue"
    CANCEL_RUN = "cancel-run"
    SKIP_THIS_VERSION = "skip-this-version"


@dataclass(frozen=True)
class UpdatePrompt:
    """Everything the user needs to decide, and nothing they do not."""

    cli_type: str
    current_version: str | None
    latest_version: str | None
    install_source: str
    detail: str

    def question(self) -> str:
        current = self.current_version or "unknown"
        latest = self.latest_version or "a newer version"
        return f"{self.cli_type} {current} → {latest} is available. Update before running?"


#: Returns the user's choice. ``None`` means "could not ask" (non-interactive), which is treated as
#: CONTINUE_WITHOUT_UPDATING rather than as a stall.
UpdatePromptCallback = Callable[[UpdatePrompt], UpdateChoice | None]


@dataclass(frozen=True)
class UpdateDecision:
    choice: UpdateChoice
    #: True when no callback was available, so the policy degraded to NOTIFY.
    degraded: bool = False
    detail: str = ""


def _fingerprint(installation: CliInstallation) -> str:
    """Identify the exact binary a suppression applies to.

    Size and mtime, not a content hash: hashing a 100 MB binary on every preflight would be a
    visible cost for a check that runs before every single run. The point is only to detect that
    the binary changed, and a replaced executable essentially never keeps both.
    """

    try:
        stat = Path(installation.executable).stat()
        raw = f"{installation.executable}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        raw = installation.executable
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def suppression_key(installation: CliInstallation, status: CliUpdateStatus) -> str:
    """The identity of one specific question, so an answer cannot outlive it."""

    return ":".join(
        (
            installation.type,
            status.current_version or "unknown",
            status.latest_version or "unknown",
            _fingerprint(installation),
        )
    )


#: How long a "do not ask again" answer stays in effect before it is asked again. Long enough to be
#: useful across a project, short enough that a stale suppression for a version the user has since
#: moved past does not linger forever.
_SUPPRESSION_TTL = timedelta(days=90)
#: Bounded so the file cannot grow without limit; eviction is by age, not by hash order.
_SUPPRESSION_MAX_ENTRIES = 256
#: Brief, because the critical section is a few KiB of JSON. A wait means another process is mid-write.
_SUPPRESSION_LOCK_TIMEOUT = 5.0


class UpdatePromptSuppressions:
    """Persisted "do not ask again for this version" answers.

    Each record is ``{fingerprint, created_at, expires_at}`` — the fingerprint is an opaque answered-
    question identity (no version or path in readable form), the timestamps drive expiry and bounded
    eviction. The read-modify-write runs under a cross-process lock and re-reads inside it, so two
    ``openagent`` processes dismissing prompts at once cannot lose each other's entry (spec §14). A
    malformed file resets to empty rather than crashing, and its raw content is never echoed.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def _lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def _load(self) -> list[dict[str, str]]:
        """Every record on disk, migrating the pre-timestamp flat-string format forward."""

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            # Malformed or missing: treat as empty. Never surface the raw bytes — they are opaque
            # fingerprints, and echoing a corrupt config is both useless and a disclosure risk.
            return []
        if not isinstance(raw, list):
            return []
        now = datetime.now(timezone.utc)
        records: list[dict[str, str]] = []
        for item in raw:
            if isinstance(item, str):
                # Legacy flat key: adopt it with a fresh window rather than dropping the user's answer.
                records.append(_suppression_record(item, now))
            elif isinstance(item, dict) and isinstance(item.get("fingerprint"), str):
                records.append({str(k): str(v) for k, v in item.items()})
        return records

    def _active(self, records: list[dict[str, str]], now: datetime) -> list[dict[str, str]]:
        active: list[dict[str, str]] = []
        for record in records:
            expires = record.get("expires_at")
            if expires:
                try:
                    if datetime.fromisoformat(expires) <= now:
                        continue  # expired: it will be asked again
                except ValueError:
                    pass
            active.append(record)
        return active

    def contains(self, key: str) -> bool:
        now = datetime.now(timezone.utc)
        return any(r.get("fingerprint") == key for r in self._active(self._load(), now))

    def add(self, key: str) -> None:
        try:
            with file_lock(self._lock_path(), timeout=_SUPPRESSION_LOCK_TIMEOUT):
                now = datetime.now(timezone.utc)
                # Re-read *inside* the lock so a concurrent add is not lost, and drop expired entries.
                records = self._active(self._load(), now)
                records = [r for r in records if r.get("fingerprint") != key]
                records.append(_suppression_record(key, now))
                # Bounded eviction by age: keep the most-recently-created, not the hash-alphabetical
                # tail the old ``sorted(keys)[-256:]`` produced.
                records.sort(key=lambda r: r.get("created_at") or "")
                records = records[-_SUPPRESSION_MAX_ENTRIES:]
                atomic_write_text(self.path, json.dumps(records, indent=2), mode=0o600)
        except LockTimeout:
            # A suppression is a convenience, not a correctness guarantee: if another process holds
            # the lock, the worst case is that this prompt is asked again. Never block the run on it.
            return


def _suppression_record(fingerprint: str, now: datetime) -> dict[str, str]:
    return {
        "fingerprint": fingerprint,
        "created_at": now.isoformat(),
        "expires_at": (now + _SUPPRESSION_TTL).isoformat(),
    }


def is_non_interactive(environ: dict[str, str] | None = None) -> bool:
    """Whether asking a question here would hang rather than be answered.

    CI systems set ``CI``; OpenAgent honours an explicit opt-out too. This is a hint used to skip
    the callback entirely — the callback returning ``None`` remains the authoritative signal, since
    only it knows whether a UI is actually attached.
    """

    env = os.environ if environ is None else environ
    if env.get("OPENAGENT_NON_INTERACTIVE", "").strip().lower() in {"1", "true", "yes"}:
        return True
    return env.get("CI", "").strip().lower() in {"1", "true", "yes"}


def decide_update(
    installation: CliInstallation,
    status: CliUpdateStatus,
    *,
    callback: UpdatePromptCallback | None,
    suppressions: UpdatePromptSuppressions | None = None,
    environ: dict[str, str] | None = None,
) -> UpdateDecision:
    """Resolve an available update under ``ASK``.

    ``NEVER``, ``NOTIFY`` and ``AUTO`` are decided by the caller; this function owns the branch that
    previously did not exist.
    """

    key = suppression_key(installation, status)
    if suppressions is not None and suppressions.contains(key):
        return UpdateDecision(
            UpdateChoice.CONTINUE_WITHOUT_UPDATING,
            detail="you chose not to be asked again about this version",
        )

    if callback is None or is_non_interactive(environ):
        return UpdateDecision(
            UpdateChoice.CONTINUE_WITHOUT_UPDATING,
            degraded=True,
            detail=(
                "an update is available but this session cannot prompt; "
                "continuing with the installed version"
            ),
        )

    prompt = UpdatePrompt(
        cli_type=installation.type,
        current_version=status.current_version,
        latest_version=status.latest_version,
        install_source=installation.install_source.value,
        detail=status.detail,
    )
    try:
        choice = callback(prompt)
    except Exception as exc:  # noqa: BLE001 - a broken prompt must not take the run with it
        return UpdateDecision(
            UpdateChoice.CONTINUE_WITHOUT_UPDATING,
            degraded=True,
            detail=f"the update prompt failed ({exc}); continuing with the installed version",
        )

    if choice is None:
        return UpdateDecision(
            UpdateChoice.CONTINUE_WITHOUT_UPDATING,
            degraded=True,
            detail="no answer was available; continuing with the installed version",
        )

    if choice is UpdateChoice.SKIP_THIS_VERSION and suppressions is not None:
        suppressions.add(key)

    return UpdateDecision(choice)
