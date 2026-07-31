"""Cross-process safe, durable JSON object storage for global configuration."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .atomic import atomic_write_text
from .file_lock import LockTimeout, file_lock

DEFAULT_MAX_BYTES = 1024 * 1024
DEFAULT_LOCK_TIMEOUT = 5.0
SCHEMA_VERSION = 1


class LockedJsonStoreError(RuntimeError):
    """Base class for safe configuration-store failures."""


class LockedJsonStoreTimeout(LockedJsonStoreError):
    """Another process held the configuration lock beyond the bounded wait."""


class LockedJsonStore:
    """An object-root JSON store with locked read-modify-write and recoverable quarantine."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.lock_timeout = lock_timeout

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def read(self) -> dict[str, Any]:
        try:
            with file_lock(self.lock_path, timeout=self.lock_timeout):
                value, _encoded = self._load_locked(quarantine=True)
                return value
        except LockTimeout as exc:
            raise LockedJsonStoreTimeout("timed out waiting for the configuration lock") from exc

    def get_section(self, name: str, default: Any = None) -> Any:
        return self.read().get(name, default)

    def update(self, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        try:
            with file_lock(self.lock_path, timeout=self.lock_timeout):
                value, before = self._load_locked(quarantine=True)
                mutator(value)
                value.setdefault("schema_version", SCHEMA_VERSION)
                encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                if len(encoded.encode("utf-8")) > self.max_bytes:
                    raise LockedJsonStoreError("configuration exceeds the safe size limit")
                if before != encoded:
                    atomic_write_text(self.path, encoded, mode=0o600)
                return value
        except LockTimeout as exc:
            raise LockedJsonStoreTimeout("timed out waiting for the configuration lock") from exc

    def update_section(self, name: str, value: Any) -> dict[str, Any]:
        self._validate_section_name(name)

        def replace(document: dict[str, Any]) -> None:
            document[name] = value

        return self.update(replace)

    def migrate_legacy_list(self, section: str) -> None:
        """Atomically wrap a legacy list-root document in this store's object schema."""

        self._validate_section_name(section)
        try:
            with file_lock(self.lock_path, timeout=self.lock_timeout):
                try:
                    info = self.path.lstat()
                    if not self.path.is_file() or self.path.is_symlink():
                        raise LockedJsonStoreError("configuration path is not a regular file")
                    if info.st_size > self.max_bytes:
                        return
                    parsed = json.loads(self.path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    return
                except LockedJsonStoreError:
                    raise
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    # Normal reads perform the established quarantine without exposing raw bytes.
                    self._load_locked(quarantine=True)
                    return
                if not isinstance(parsed, list):
                    return
                migrated = {
                    "schema_version": SCHEMA_VERSION,
                    section: parsed,
                }
                encoded = json.dumps(migrated, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                if len(encoded.encode("utf-8")) > self.max_bytes:
                    raise LockedJsonStoreError("configuration exceeds the safe size limit")
                atomic_write_text(self.path, encoded, mode=0o600)
        except LockTimeout as exc:
            raise LockedJsonStoreTimeout("timed out waiting for the configuration lock") from exc

    @staticmethod
    def _validate_section_name(name: str) -> None:
        if not isinstance(name, str) or not name or name == "schema_version":
            raise LockedJsonStoreError("invalid configuration section name")

    def _load_locked(self, *, quarantine: bool) -> tuple[dict[str, Any], str | None]:
        try:
            info = self.path.lstat()
            if not self.path.is_file() or self.path.is_symlink():
                raise LockedJsonStoreError("configuration path is not a regular file")
            if info.st_size > self.max_bytes:
                raise ValueError("oversized")
            raw_bytes = self.path.read_bytes()
            raw = raw_bytes.decode("utf-8")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("root is not an object")
            version = parsed.get("schema_version", SCHEMA_VERSION)
            if not isinstance(version, int) or version < 1:
                raise ValueError("invalid schema version")
            parsed.setdefault("schema_version", SCHEMA_VERSION)
            canonical = json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            return parsed, canonical
        except FileNotFoundError:
            return {"schema_version": SCHEMA_VERSION}, None
        except LockedJsonStoreError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            if quarantine and self.path.exists():
                target = self.path.with_name(
                    f"{self.path.name}.corrupt.{int(time.time())}.{uuid.uuid4().hex[:8]}"
                )
                os.replace(self.path, target)
                try:
                    os.chmod(target, 0o600)
                except OSError:
                    pass
            return {"schema_version": SCHEMA_VERSION}, None
