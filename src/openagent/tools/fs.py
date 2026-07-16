"""Filesystem tools (spec §2.1, §27).

All paths are workspace-relative and validated by :meth:`ToolContext.resolve_path`. Edits prefer
``apply_patch`` (targeted, reviewable) over ``write_file`` (spec §27).
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from .base import ToolContext, ToolError, ToolResult

_MAX_READ_BYTES = 200_000
_IGNORE_DIRS = {".git", ".openagent", ".venv", "node_modules", "__pycache__", ".mypy_cache"}


def list_files(ctx: ToolContext, path: str = ".", depth: int = 2) -> ToolResult:
    root = ctx.resolve_path(path)
    if not root.exists():
        raise ToolError(f"{path} does not exist")
    entries: list[str] = []
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        current = Path(dirpath)
        if len(current.parts) - base_depth >= depth:
            dirnames[:] = []
        for name in sorted(filenames):
            rel = (current / name).relative_to(ctx.workspace_root.resolve())
            entries.append(str(rel))
    return ToolResult.success("\n".join(entries[:1000]), count=len(entries))


def read_file(ctx: ToolContext, path: str) -> ToolResult:
    target = ctx.resolve_path(path)
    if not target.is_file():
        raise ToolError(f"{path} is not a file")
    data = target.read_bytes()[:_MAX_READ_BYTES]
    text = data.decode("utf-8", errors="replace")
    return ToolResult.success(text, path=path, bytes=len(data))


def search_files(ctx: ToolContext, pattern: str) -> ToolResult:
    root = ctx.workspace_root.resolve()
    matches: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        for name in filenames:
            if fnmatch.fnmatch(name, pattern):
                matches.append(str((Path(dirpath) / name).relative_to(root)))
    return ToolResult.success("\n".join(sorted(matches)[:500]), count=len(matches))


def search_text(ctx: ToolContext, query: str, glob: str = "*") -> ToolResult:
    root = ctx.workspace_root.resolve()
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        for name in filenames:
            if not fnmatch.fnmatch(name, glob):
                continue
            fpath = Path(dirpath) / name
            try:
                for i, line in enumerate(
                    fpath.read_text(encoding="utf-8", errors="replace").splitlines(), 1
                ):
                    if query in line:
                        rel = fpath.relative_to(root)
                        hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                        if len(hits) >= 500:
                            break
            except OSError:
                continue
    return ToolResult.success("\n".join(hits), count=len(hits))


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
