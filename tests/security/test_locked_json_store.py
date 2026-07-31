"""Persistent JSON sections use one cross-process, durable read-modify-write primitive."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from openagent.security.file_lock import LockTimeout
from openagent.security.locked_json_store import (
    LockedJsonStore,
    LockedJsonStoreError,
    LockedJsonStoreTimeout,
)

pytestmark = [pytest.mark.security, pytest.mark.multiprocess]


_WORKER = textwrap.dedent(
    """
    import json
    import sys
    import time
    from pathlib import Path

    from openagent.security.locked_json_store import LockedJsonStore

    path = Path(sys.argv[1])
    section = sys.argv[2]
    value = sys.argv[3]
    ready = Path(sys.argv[4])
    go = Path(sys.argv[5])
    ready.write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 20
    while not go.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("timed out waiting for barrier")
        time.sleep(0.01)
    LockedJsonStore(path).update_section(section, {"value": value})
    print(json.dumps({"section": section}))
    """
)


def test_two_processes_updating_different_sections_do_not_lose_data(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"schema_version": 1, "unknown_extension": {"keep": True}}),
        encoding="utf-8",
    )
    script = tmp_path / "worker.py"
    script.write_text(_WORKER, encoding="utf-8")
    go = tmp_path / "go"
    processes = []
    for index, section in enumerate(("cli_updates", "user_preferences")):
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(script),
                    str(path),
                    section,
                    f"value-{index}",
                    str(tmp_path / f"ready-{index}"),
                    str(go),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={
                    **os.environ,
                    "PYTHONPATH": str(Path("src").resolve())
                    + os.pathsep
                    + os.environ.get("PYTHONPATH", ""),
                },
            )
        )
    deadline = __import__("time").monotonic() + 20
    while not all((tmp_path / f"ready-{index}").exists() for index in range(2)):
        if __import__("time").monotonic() >= deadline:
            raise AssertionError("workers did not reach barrier")
        __import__("time").sleep(0.01)
    go.write_text("go", encoding="utf-8")

    for process in processes:
        _stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 1
    assert saved["unknown_extension"] == {"keep": True}
    assert saved["cli_updates"] == {"value": "value-0"}
    assert saved["user_preferences"] == {"value": "value-1"}
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


def test_malformed_store_is_quarantined_without_echoing_contents(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    raw = "{ malformed prefixless-sensitive-content"
    path.write_text(raw, encoding="utf-8")

    LockedJsonStore(path).update_section("cli_updates", {"policy": "ask"})

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["cli_updates"] == {"policy": "ask"}
    quarantined = list(tmp_path.glob("config.json.corrupt.*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == raw


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        '{"schema_version": 0}',
        '{"schema_version": "one"}',
        "\\udcff",
    ],
)
def test_invalid_object_roots_are_quarantined(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "config.json"
    if payload == "\\udcff":
        path.write_bytes(b"\xff")
    else:
        path.write_text(payload, encoding="utf-8")

    assert LockedJsonStore(path).read() == {"schema_version": 1}
    assert len(list(tmp_path.glob("config.json.corrupt.*"))) == 1


def test_store_rejects_unsafe_paths_sections_and_oversized_updates(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(LockedJsonStoreError, match="regular file"):
        LockedJsonStore(directory).read()

    path = tmp_path / "config.json"
    store = LockedJsonStore(path, max_bytes=64)
    for invalid in ("", "schema_version"):
        with pytest.raises(LockedJsonStoreError, match="section"):
            store.update_section(invalid, {})
    with pytest.raises(LockedJsonStoreError, match="size"):
        store.update_section("large", "x" * 100)


def test_legacy_list_migration_is_bounded_idempotent_and_preserves_nonlists(tmp_path: Path) -> None:
    path = tmp_path / "prompts.json"
    store = LockedJsonStore(path)
    store.migrate_legacy_list("prompts")  # missing is a no-op

    path.write_text(json.dumps(["a", "b"]), encoding="utf-8")
    store.migrate_legacy_list("prompts")
    assert store.get_section("prompts") == ["a", "b"]
    before = path.read_text(encoding="utf-8")
    store.migrate_legacy_list("prompts")
    assert path.read_text(encoding="utf-8") == before


def test_store_maps_lock_timeouts_to_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    @contextmanager
    def timed_out(*_args, **_kwargs):
        raise LockTimeout("busy")
        yield

    monkeypatch.setattr("openagent.security.locked_json_store.file_lock", timed_out)
    store = LockedJsonStore(tmp_path / "config.json")
    with pytest.raises(LockedJsonStoreTimeout):
        store.read()
    with pytest.raises(LockedJsonStoreTimeout):
        store.update_section("section", {})
    with pytest.raises(LockedJsonStoreTimeout):
        store.migrate_legacy_list("section")
