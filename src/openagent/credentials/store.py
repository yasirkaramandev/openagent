"""Credential store (spec §30).

Secrets are resolved through a :class:`CredentialRef` — a pointer, never the value. Four backends:

* ``keychain`` — OS keychain via :mod:`keyring` (default).
* ``env`` — read from an environment variable at use time.
* ``session`` — held in memory for this process only.
* ``external-command`` — shell out to a user-provided command that prints the secret.

Secrets are never persisted to the DB, logs, events, or command arguments. For CLI subprocesses the
resolved value is injected only into the child's environment (see security/process.py).
"""

from __future__ import annotations

import os
import subprocess

from ..core.models import CredentialRef, CredentialType

try:  # keyring is optional at import time so unit tests run without a backend
    import keyring
except Exception:  # pragma: no cover - environment dependent
    keyring = None  # type: ignore[assignment]


class CredentialError(RuntimeError):
    pass


class CredentialStore:
    """Resolve and persist secrets by reference."""

    def __init__(self, service: str = "openagent") -> None:
        self.service = service
        self._session: dict[str, str] = {}

    # ------------------------------------------------------------------ writing

    def set_secret(self, ref: CredentialRef, secret: str) -> None:
        """Persist a secret for later resolution via ``ref``.

        Only ``keychain`` and ``session`` are writable here; ``env``/``external-command`` refer to
        values the user manages themselves.
        """

        if ref.type is CredentialType.KEYCHAIN:
            if keyring is None:
                raise CredentialError("keyring backend unavailable")
            keyring.set_password(ref.service or self.service, ref.account or "", secret)
        elif ref.type is CredentialType.SESSION:
            self._session[self._session_key(ref)] = secret
        else:
            raise CredentialError(
                f"cannot store secrets for credential type {ref.type.value!r}"
            )

    def delete_secret(self, ref: CredentialRef) -> None:
        if ref.type is CredentialType.KEYCHAIN and keyring is not None:
            try:
                keyring.delete_password(ref.service or self.service, ref.account or "")
            except Exception:  # pragma: no cover - best effort
                pass
        self._session.pop(self._session_key(ref), None)

    # ------------------------------------------------------------------ reading

    def resolve(self, ref: CredentialRef) -> str | None:
        """Return the secret ``ref`` points to, or ``None`` when not required/available."""

        if ref.type is CredentialType.NONE:
            return None
        if ref.type is CredentialType.ENV:
            if not ref.env_var:
                raise CredentialError("env credential requires env_var")
            return os.environ.get(ref.env_var)
        if ref.type is CredentialType.SESSION:
            return self._session.get(self._session_key(ref))
        if ref.type is CredentialType.EXTERNAL_COMMAND:
            if not ref.command:
                raise CredentialError("external-command credential requires command")
            result = subprocess.run(  # noqa: S603 - user-provided by design
                ref.command, capture_output=True, text=True, check=False
            )
            if result.returncode != 0:
                raise CredentialError(
                    f"credential command failed with exit {result.returncode}"
                )
            return result.stdout.strip()
        if ref.type is CredentialType.KEYCHAIN:
            if keyring is None:
                raise CredentialError("keyring backend unavailable")
            try:
                return keyring.get_password(ref.service or self.service, ref.account or "")
            except Exception:  # noqa: BLE001 - no usable backend (headless CI/servers): treat as "no key"
                return None
        raise CredentialError(f"unknown credential type {ref.type!r}")

    def available(self, ref: CredentialRef) -> bool:
        try:
            return self.resolve(ref) is not None
        except CredentialError:
            return False

    @staticmethod
    def _session_key(ref: CredentialRef) -> str:
        return f"{ref.service}/{ref.account or ''}"


def keychain_available() -> bool:
    """Whether an OS keychain backend is usable (spec §41 doctor check)."""

    if keyring is None:
        return False
    try:
        backend = keyring.get_keyring()
        name = backend.__class__.__name__.lower()
        return "fail" not in name and "null" not in name
    except Exception:  # pragma: no cover - environment dependent
        return False
