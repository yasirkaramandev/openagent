"""Filesystem tools (spec §2.1, §27, §4, §11).

All paths are workspace-relative and validated by :meth:`ToolContext.resolve_path`. Edits prefer
``apply_patch`` (targeted, reviewable) over ``write_file`` (spec §27).

Two properties the recursive tools must hold, which they previously did not:

**They stay inside the workspace (§4).** ``read_file`` was always safe because ``resolve_path``
resolves symlinks and rejects escapes. The walkers were not: they used ``os.walk`` paths directly, so
a symlink in the workspace pointing at ``/etc/passwd`` was opened and its contents returned. Every
candidate is now resolved and re-checked against the root, and non-regular files (FIFO, socket,
device) are skipped — reading a FIFO blocks forever, and ``/dev/zero`` never ends.

**They are bounded (§11).** ``search_text`` did ``read_text().splitlines()``, materialising a whole
file *and* a list of its lines; the walk had no file, byte, result, time or cancellation limit; and
the 500-hit ``break`` escaped only the inner loop, so it kept walking everything anyway. Scans now
stream line by line and stop at explicit limits, reporting ``truncated``/``cancelled`` rather than
silently returning a partial answer that looks complete.
"""

from __future__ import annotations

import fnmatch
import os
import time
from collections.abc import Iterator
from pathlib import Path

from .base import ToolContext, ToolError, ToolResult

_MAX_READ_BYTES = 200_000
_IGNORE_DIRS = {".git", ".openagent", ".venv", "node_modules", "__pycache__", ".mypy_cache"}

# --------------------------------------------------------------------------- bounds (§11)

#: Skip any single file bigger than this when scanning content.
MAX_SCAN_FILE_BYTES = 2_000_000
#: Stop reading a file's content after this much has been consumed.
MAX_SCAN_TOTAL_BYTES = 64_000_000
#: Default caps. Callers may lower them; nothing may raise them past a full walk.
MAX_RESULTS = 500
MAX_LIST_RESULTS = 1000
MAX_FILES_SCANNED = 20_000
#: Wall-clock budget for one scan.
SCAN_DEADLINE_SECONDS = 20.0
#: A line longer than this is truncated in the *output* (the file is still streamed, never slurped).
_MAX_LINE_CHARS = 200


class _Budget:
    """Shared stop conditions for one scan: files, bytes, results, deadline, cancellation."""

    def __init__(
        self,
        ctx: ToolContext,
        *,
        max_results: int,
        max_files: int,
        deadline: float = SCAN_DEADLINE_SECONDS,
    ) -> None:
        self.ctx = ctx
        self.max_results = max_results
        self.max_files = max_files
        self.files_scanned = 0
        self.bytes_read = 0
        self.truncated = False
        self.cancelled = False
        self._end = time.monotonic() + deadline

    def stop(self) -> bool:
        """True when the scan must end. Sets the reason so the caller can report it honestly."""

        cancellation = getattr(self.ctx, "cancellation", None)
        if cancellation is not None and cancellation.cancelled:
            self.cancelled = True
            return True
        if self.files_scanned >= self.max_files:
            self.truncated = True
            return True
        if self.bytes_read >= MAX_SCAN_TOTAL_BYTES:
            self.truncated = True
            return True
        if time.monotonic() >= self._end:
            self.truncated = True
            return True
        return False

    def data(self, **extra: object) -> dict:
        return {
            "truncated": self.truncated,
            "cancelled": self.cancelled,
            "files_scanned": self.files_scanned,
            "bytes_read": self.bytes_read,
            **extra,
        }


def _is_safe_regular_file(path: Path, root: Path) -> bool:
    """Whether ``path`` is a real file that genuinely lives inside ``root`` (§4).

    Resolves symlinks and re-checks containment, then insists on a *regular* file: a FIFO blocks
    forever on read, a socket is not readable, and a device like ``/dev/zero`` never ends. ``lstat``
    first so a dangling or looping symlink is a cheap skip rather than an exception.
    """

    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError, RuntimeError):
        return False  # escapes the workspace, is a broken/looping link, or cannot be resolved
    try:
        return resolved.is_file() and os.path.isfile(resolved) and not resolved.is_symlink()
    except OSError:
        return False


def _walk(root: Path, budget: _Budget, *, depth: int | None = None) -> Iterator[Path]:
    """Walk ``root`` without following directory symlinks, honouring the budget.

    ``followlinks=False`` (the default) already prevents recursing into a symlinked directory, which
    is also what stops a symlink loop from spinning forever; each candidate file is still resolved and
    containment-checked, because a symlinked *file* is followed on open.
    """

    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if budget.stop():
            return
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        current = Path(dirpath)
        if depth is not None and len(current.parts) - base_depth >= depth:
            dirnames[:] = []
        for name in sorted(filenames):
            if budget.stop():
                return
            candidate = current / name
            if not _is_safe_regular_file(candidate, root):
                continue
            budget.files_scanned += 1
            yield candidate


def list_files(
    ctx: ToolContext, path: str = ".", depth: int = 2, max_results: int = MAX_LIST_RESULTS
) -> ToolResult:
    root = ctx.resolve_path(path)
    if not root.exists():
        raise ToolError(f"{path} does not exist")
    workspace = ctx.workspace_root.resolve()
    budget = _Budget(ctx, max_results=max_results, max_files=MAX_FILES_SCANNED)
    entries: list[str] = []
    for file in _walk(root, budget, depth=depth):
        if len(entries) >= max_results:
            budget.truncated = True
            break
        entries.append(str(file.resolve().relative_to(workspace)))
    return ToolResult.success("\n".join(entries), count=len(entries), **budget.data())


def read_file(ctx: ToolContext, path: str) -> ToolResult:
    target = ctx.resolve_path(path)
    if not target.is_file():
        raise ToolError(f"{path} is not a file")
    if not _is_safe_regular_file(target, ctx.workspace_root.resolve()):
        raise ToolError(f"{path} is not a regular file")
    data = target.read_bytes()[:_MAX_READ_BYTES]
    text = data.decode("utf-8", errors="replace")
    return ToolResult.success(text, path=path, bytes=len(data))


def search_files(
    ctx: ToolContext,
    pattern: str,
    max_results: int = MAX_RESULTS,
    max_files: int = MAX_FILES_SCANNED,
) -> ToolResult:
    root = ctx.workspace_root.resolve()
    budget = _Budget(ctx, max_results=max_results, max_files=max_files)
    matches: list[str] = []
    for file in _walk(root, budget):
        if fnmatch.fnmatch(file.name, pattern):
            if len(matches) >= max_results:
                budget.truncated = True
                break
            matches.append(str(file.resolve().relative_to(root)))
    return ToolResult.success("\n".join(sorted(matches)), count=len(matches), **budget.data())


def _scan_lines(path: Path, query: str, rel: str, budget: _Budget) -> Iterator[str]:
    """Stream one file line by line, never materialising it (§11)."""

    try:
        size = path.stat().st_size
    except OSError:
        return
    if size > MAX_SCAN_FILE_BYTES:
        budget.truncated = True  # too big to scan — say so rather than pretend it had no match
        return
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, 1):
                budget.bytes_read += len(line)
                if budget.stop():
                    return
                if query in line:
                    yield f"{rel}:{number}: {line.strip()[:_MAX_LINE_CHARS]}"
    except (OSError, ValueError):
        return  # unreadable/binary-ish — skip it, never fail the whole scan


def _looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\0" in handle.read(4096)
    except OSError:
        return True


def search_text(
    ctx: ToolContext,
    query: str,
    glob: str = "*",
    max_results: int = MAX_RESULTS,
    max_files: int = MAX_FILES_SCANNED,
) -> ToolResult:
    root = ctx.workspace_root.resolve()
    budget = _Budget(ctx, max_results=max_results, max_files=max_files)
    hits: list[str] = []
    for file in _walk(root, budget):
        if not fnmatch.fnmatch(file.name, glob):
            continue
        if _looks_binary(file):
            continue
        rel = str(file.resolve().relative_to(root))
        for hit in _scan_lines(file, query, rel, budget):
            hits.append(hit)
            if len(hits) >= max_results:
                budget.truncated = True
                break
        # The old code's `break` escaped only the inner loop and kept walking every remaining file.
        if len(hits) >= max_results or budget.stop():
            break
    return ToolResult.success("\n".join(hits), count=len(hits), **budget.data())


def write_file(ctx: ToolContext, path: str, content: str) -> ToolResult:
    if not ctx.profile.can_edit_files:
        raise ToolError("this permission profile does not allow file edits")
    target = ctx.resolve_path(path)
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    if ctx.emit:
        ctx.emit("file.modified" if existed else "file.created", {"path": path})
    return ToolResult.success(
        f"wrote {len(content)} bytes to {path}", path=path, created=not existed
    )


def apply_patch(
    ctx: ToolContext, path: str, old_string: str, new_string: str, replace_all: bool = False
) -> ToolResult:
    """Targeted edit: replace ``old_string`` with ``new_string`` in ``path``.

    Reliable and reviewable (small diffs), preferred over ``write_file`` (spec §27). ``old_string``
    must be unique unless ``replace_all`` is set.
    """

    if not ctx.profile.can_edit_files:
        raise ToolError("this permission profile does not allow file edits")
    target = ctx.resolve_path(path)
    if not target.is_file():
        raise ToolError(f"{path} is not a file")
    text = target.read_text(encoding="utf-8")
    count = text.count(old_string)
    if count == 0:
        raise ToolError("old_string not found in file")
    if count > 1 and not replace_all:
        raise ToolError(
            f"old_string is not unique ({count} matches); set replace_all or add context"
        )
    updated = (
        text.replace(old_string, new_string)
        if replace_all
        else text.replace(old_string, new_string, 1)
    )
    target.write_text(updated, encoding="utf-8")
    if ctx.emit:
        ctx.emit("file.modified", {"path": path})
    return ToolResult.success(
        f"patched {path} ({count if replace_all else 1} replacement(s))", path=path
    )
