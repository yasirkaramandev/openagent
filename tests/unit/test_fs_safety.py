"""Filesystem tools stay inside the workspace and stay bounded (spec §4, §11).

``read_file`` was safe: it goes through ``ToolContext.resolve_path``, which resolves symlinks and
rejects anything landing outside the workspace. The *recursive* tools did not — ``list_files``,
``search_files`` and ``search_text`` walked with ``os.walk`` and used the paths directly, so:

* a symlink inside the workspace pointing at ``/etc/passwd`` (or anywhere else) was **read and
  returned** by ``search_text`` — verified: the host file's contents came back in the tool result;
* a FIFO in the workspace made ``read_text()`` block forever — a trivial denial of service;
* nothing was bounded: ``search_text`` did ``read_text().splitlines()``, materialising an entire file
  *and* a list of all its lines, so one big file (or a multi-GB single line) exhausted memory. The
  walk itself had no file-count, byte, result, deadline or cancellation limit, and the 500-hit
  ``break`` only escaped the inner loop — it kept walking every remaining file.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

from openagent.core.cancellation import RunCancellation
from openagent.core.permissions import SAFE_EDIT, get_profile
from openagent.security.approvals import ApprovalGate
from openagent.tools.base import ToolContext, ToolError
from openagent.tools.fs import list_files, read_file, search_files, search_text

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="POSIX symlink/FIFO/socket semantics"
)

_SECRET = "TOP_SECRET_HOST_CONTENT_12345"


def _ctx(root: Path, cancellation: RunCancellation | None = None) -> ToolContext:
    return ToolContext(
        workspace_root=root,
        profile=get_profile(SAFE_EDIT),
        approval_gate=ApprovalGate(auto_approve=False),
        run_id="run_fs",
        cancellation=cancellation,
    )


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "real.txt").write_text("ordinary in-workspace content\n")
    return workspace


# --------------------------------------------------------------------------- §4 symlink escapes


def test_search_text_does_not_follow_a_symlink_out_of_the_workspace(ws: Path, tmp_path: Path):
    outside = tmp_path / "outside_secret.txt"
    outside.write_text(f"{_SECRET}\n")
    (ws / "link.txt").symlink_to(outside)

    result = search_text(_ctx(ws), _SECRET)
    assert _SECRET not in result.content, "search_text exfiltrated a file outside the workspace"


def test_search_text_does_not_follow_a_symlink_to_etc_passwd(ws: Path):
    passwd = Path("/etc/passwd")
    if not passwd.exists():
        pytest.skip("/etc/passwd not present")
    (ws / "passwd_link").symlink_to(passwd)
    result = search_text(_ctx(ws), "root")
    assert "passwd_link" not in result.content


def test_list_files_does_not_list_a_symlink_escaping_the_workspace(ws: Path, tmp_path: Path):
    outside = tmp_path / "outside_secret.txt"
    outside.write_text(f"{_SECRET}\n")
    (ws / "link.txt").symlink_to(outside)

    listed = list_files(_ctx(ws), ".").content
    assert "link.txt" not in listed
    assert "real.txt" in listed, "ordinary files must still be listed"


def test_search_files_does_not_match_an_escaping_symlink(ws: Path, tmp_path: Path):
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    (ws / "escape.txt").symlink_to(outside)
    assert "escape.txt" not in search_files(_ctx(ws), "*.txt").content


def test_symlink_to_an_external_directory_is_not_walked(ws: Path, tmp_path: Path):
    external = tmp_path / "external"
    external.mkdir()
    (external / "hidden.txt").write_text(f"{_SECRET}\n")
    (ws / "dirlink").symlink_to(external, target_is_directory=True)

    assert _SECRET not in search_text(_ctx(ws), _SECRET).content
    assert "hidden.txt" not in list_files(_ctx(ws), ".", depth=5).content


def test_symlink_inside_the_workspace_is_still_usable(ws: Path):
    """The clamp must not break a legitimate in-workspace symlink."""

    (ws / "inner.txt").write_text("inner content here\n")
    (ws / "alias.txt").symlink_to(ws / "inner.txt")
    assert "inner content here" in search_text(_ctx(ws), "inner content").content


def test_symlink_loop_terminates(ws: Path):
    """A → B → A must not hang or recurse forever."""

    a = ws / "a"
    b = ws / "b"
    a.symlink_to(b)
    b.symlink_to(a)
    # Must simply return (the loop resolves to nothing usable), not hang or raise.
    list_files(_ctx(ws), ".")
    search_text(_ctx(ws), "anything")
    search_files(_ctx(ws), "*")


def test_self_referential_directory_symlink_terminates(ws: Path):
    nested = ws / "nested"
    nested.mkdir()
    (nested / "self").symlink_to(ws, target_is_directory=True)
    list_files(_ctx(ws), ".", depth=10)
    search_text(_ctx(ws), "anything")


def test_read_file_still_rejects_an_escaping_symlink(ws: Path, tmp_path: Path):
    outside = tmp_path / "outside_secret.txt"
    outside.write_text(f"{_SECRET}\n")
    (ws / "link.txt").symlink_to(outside)
    with pytest.raises(ToolError, match="escapes the workspace"):
        read_file(_ctx(ws), "link.txt")


# --------------------------------------------------------------------------- §4 special files


def test_fifo_is_not_read(ws: Path):
    """Reading a FIFO blocks forever — a one-line denial of service."""

    os.mkfifo(ws / "pipe")
    # If this hangs, the bound is missing; pytest-timeout is not assumed, so the assert is that it
    # returns at all.
    result = search_text(_ctx(ws), "anything")
    assert "pipe" not in result.content


def test_socket_is_not_read(ws: Path, monkeypatch: pytest.MonkeyPatch):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        # AF_UNIX paths are capped (~104 bytes on macOS) and pytest's tmp_path is long, so bind by a
        # short *relative* name from inside the workspace.
        monkeypatch.chdir(ws)
        sock.bind("sock")
        result = search_text(_ctx(ws), "anything")
        assert "sock" not in result.content
    finally:
        sock.close()


def test_device_files_are_not_read(ws: Path):
    """/dev/zero would stream forever; a link to it must be ignored, not consumed."""

    zero = Path("/dev/zero")
    if not zero.exists():
        pytest.skip("/dev/zero not present")
    (ws / "zero_link").symlink_to(zero)
    result = search_text(_ctx(ws), "anything")
    assert "zero_link" not in result.content


# --------------------------------------------------------------------------- §11 bounds


def test_search_text_skips_a_file_over_the_size_limit(ws: Path):
    big = ws / "big.log"
    big.write_bytes(b"needle\n" + b"x" * 12_000_000)
    result = search_text(_ctx(ws), "needle")
    assert result.data.get("truncated") or "big.log" not in result.content


def test_search_text_handles_one_enormous_line_without_loading_it(ws: Path):
    """`read_text().splitlines()` materialised the file AND every line; streaming must not."""

    (ws / "huge_line.txt").write_bytes(b"A" * 12_000_000)
    search_text(_ctx(ws), "needle")  # must return, not exhaust memory


def test_search_text_skips_binary_files(ws: Path):
    (ws / "blob.bin").write_bytes(bytes(range(256)) * 2000)
    result = search_text(_ctx(ws), "anything")
    assert "blob.bin" not in result.content


def test_scans_stop_at_the_result_limit_and_say_so(ws: Path):
    for i in range(300):
        (ws / f"f{i}.txt").write_text("needle\n" * 20)
    result = search_text(_ctx(ws), "needle", max_results=50)
    assert len([ln for ln in result.content.splitlines() if ln.strip()]) <= 50
    assert result.data.get("truncated") is True


def test_scans_stop_at_the_file_limit(ws: Path):
    for i in range(200):
        (ws / f"f{i}.txt").write_text("x\n")
    result = search_files(_ctx(ws), "*.txt", max_files=25)
    assert result.data.get("files_scanned", 0) <= 25
    assert result.data.get("truncated") is True


def test_list_files_is_bounded_and_reports_truncation(ws: Path):
    for i in range(200):
        (ws / f"f{i}.txt").write_text("x")
    result = list_files(_ctx(ws), ".", max_results=20)
    assert len([ln for ln in result.content.splitlines() if ln.strip()]) <= 20
    assert result.data.get("truncated") is True


def test_search_text_honours_cancellation(ws: Path):
    for i in range(200):
        (ws / f"f{i}.txt").write_text("needle\n")
    cancel = RunCancellation("run_fs")
    cancel.cancel("user asked to stop")
    result = search_text(_ctx(ws, cancellation=cancel), "needle")
    assert result.data.get("cancelled") is True
