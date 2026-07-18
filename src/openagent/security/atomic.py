"""Durable atomic writes shared by artifacts, exports and generated project files."""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write and durably replace ``path`` without exposing a partial file.

    The file and its containing directory are fsynced. Directory fsync is unavailable on some
    platforms/filesystems; those specific OS errors are tolerated after the atomic replace.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            with contextlib.suppress(OSError):
                os.fchmod(handle.fileno(), mode)
        os.replace(temp_path, path)
        with contextlib.suppress(OSError):
            os.chmod(path, mode)
        _fsync_directory(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def atomic_write_lines(path: Path, lines: Iterable[str], *, mode: int = 0o600) -> None:
    """Atomically replace ``path`` with ``lines``, streaming rather than buffering.

    Same durability contract as :func:`atomic_write_bytes` — random temp name in the destination
    directory, fsync, chmod, ``os.replace``, directory fsync — but the payload is consumed lazily, so
    peak memory is one line rather than the whole file. Used by the event export, where the file can
    reach tens of thousands of records and building it as a single string was the memory cost that
    came with the old quadratic rewrite.

    Each element is written followed by a newline.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for line in lines:
                handle.write(line)
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            with contextlib.suppress(OSError):
                os.fchmod(handle.fileno(), mode)
        os.replace(temp_path, path)
        with contextlib.suppress(OSError):
            os.chmod(path, mode)
        _fsync_directory(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(directory, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
