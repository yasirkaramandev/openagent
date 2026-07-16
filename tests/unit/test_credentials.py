import sys
import time
from pathlib import Path

import pytest

import openagent.credentials.store as store_mod
from openagent.core.models import CredentialRef, CredentialType
from openagent.credentials.store import CredentialError, CredentialStore


def _cmd_ref(*code_or_argv: str, py: bool = True) -> CredentialRef:
    argv = [sys.executable, "-c", *code_or_argv] if py else list(code_or_argv)
    return CredentialRef(type=CredentialType.EXTERNAL_COMMAND, command=argv)


def test_env_credential_resolves(monkeypatch):
    monkeypatch.setenv("MY_TEST_KEY", "secret-value")
    store = CredentialStore()
    ref = CredentialRef(type=CredentialType.ENV, env_var="MY_TEST_KEY")
    assert store.resolve(ref) == "secret-value"
    assert store.available(ref) is True


def test_env_credential_missing():
    store = CredentialStore()
    ref = CredentialRef(type=CredentialType.ENV, env_var="DEFINITELY_NOT_SET_123")
    assert store.resolve(ref) is None
    assert store.available(ref) is False


def test_session_credential_roundtrip():
    store = CredentialStore()
    ref = CredentialRef(type=CredentialType.SESSION, account="openai/main")
    store.set_secret(ref, "in-memory-secret")
    assert store.resolve(ref) == "in-memory-secret"
    store.delete_secret(ref)
    assert store.resolve(ref) is None


def test_none_credential():
    store = CredentialStore()
    assert store.resolve(CredentialRef(type=CredentialType.NONE)) is None


def test_keychain_roundtrip_via_memory_backend():
    store = CredentialStore()
    ref = CredentialRef(type=CredentialType.KEYCHAIN, account="provider/test")
    store.set_secret(ref, "kc-secret")
    assert store.resolve(ref) == "kc-secret"
    store.delete_secret(ref)
    assert store.resolve(ref) is None


def test_keychain_resolve_survives_backend_failure(monkeypatch):
    """On a headless host with no usable keyring backend, resolving a keychain ref returns None
    instead of raising — so a run without a stored key still proceeds (no crash)."""
    import openagent.credentials.store as store_mod

    class _Boom:
        def get_password(self, *a, **k):
            raise RuntimeError("no keyring backend available")

    monkeypatch.setattr(store_mod, "keyring", _Boom())
    store = CredentialStore()
    ref = CredentialRef(type=CredentialType.KEYCHAIN, account="provider/x")
    assert store.resolve(ref) is None


# --------------------------------------------------------------------------- external command (item 9)


def test_external_command_credential_succeeds():
    store = CredentialStore()
    assert store.resolve(_cmd_ref("print('cmd-secret')")) == "cmd-secret"


def test_external_command_does_not_inherit_parent_secrets(monkeypatch):
    """The parent's secret env (e.g. another API key) is not exposed to the command (minimal env)."""
    monkeypatch.setenv("SECRET_LEAK", "topsecret")
    store = CredentialStore()
    ref = _cmd_ref("import os; print(os.environ.get('SECRET_LEAK', ''))")
    # SECRET_LEAK is not in the minimal env, so the command prints nothing -> empty is rejected.
    with pytest.raises(CredentialError) as exc:
        store.resolve(ref)
    assert "topsecret" not in str(exc.value)


def test_external_command_empty_output_is_error():
    store = CredentialStore()
    with pytest.raises(CredentialError, match="no output"):
        store.resolve(_cmd_ref("pass"))


def test_external_command_nonzero_exit_is_error():
    store = CredentialStore()
    with pytest.raises(CredentialError, match="exit 3"):
        store.resolve(_cmd_ref("import sys; print('x'); sys.exit(3)"))


def test_external_command_error_output_not_leaked():
    store = CredentialStore()
    with pytest.raises(CredentialError) as exc:
        store.resolve(_cmd_ref("import sys; sys.stderr.write('sk-LEAKED-SECRET'); sys.exit(1)"))
    assert "sk-LEAKED-SECRET" not in str(exc.value)


def test_external_command_excessive_output_is_rejected():
    store = CredentialStore()
    with pytest.raises(CredentialError, match="exceeds"):
        store.resolve(_cmd_ref("print('A' * (20 * 1024))"))


def test_external_command_endless_output_is_bounded_and_killed(tmp_path: Path):
    """A command that never stops printing is cut off — the limit is real, not a post-hoc check.

    The old implementation ran the command to completion via ``communicate()`` and only *then*
    compared the size against the cap, so this producer would have run until it exhausted memory.
    The bound is now enforced while reading: once the cap is crossed the process tree is killed.
    The marker proves the producer did not run to completion.
    """

    marker = tmp_path / "finished.marker"
    script = (
        "import sys, pathlib\n"
        "for _ in range(2_000_000):\n"
        "    sys.stdout.write('A' * 1024)\n"
        "    sys.stdout.flush()\n"
        f"pathlib.Path(r'{marker}').write_text('finished')\n"
    )
    store = CredentialStore()
    start = time.monotonic()
    with pytest.raises(CredentialError, match="exceeds"):
        store.resolve(_cmd_ref(script))
    elapsed = time.monotonic() - start

    assert not marker.exists(), "the endless producer was allowed to run to completion"
    # ~2 GiB of output would take far longer than this if it were actually being buffered.
    assert elapsed < 20, f"bounded read took {elapsed:.1f}s — output is not being cut off early"


def test_external_command_stderr_flood_is_also_bounded():
    """The cap covers stderr too — a secret-printing command cannot flood memory through stderr."""

    store = CredentialStore()
    script = (
        "import sys\n"
        "sys.stdout.write('sk-real-key')\n"
        "for _ in range(200_000):\n"
        "    sys.stderr.write('B' * 1024)\n"
    )
    with pytest.raises(CredentialError, match="exceeds"):
        store.resolve(_cmd_ref(script))


def test_external_command_timeout_is_error(monkeypatch):
    monkeypatch.setattr(store_mod, "CRED_CMD_TIMEOUT_SECONDS", 1)
    store = CredentialStore()
    with pytest.raises(CredentialError, match="timed out"):
        store.resolve(_cmd_ref("import time; time.sleep(30)"))


def test_external_command_timeout_kills_child_process(tmp_path: Path, monkeypatch):
    """On timeout the whole process tree is terminated — a spawned child never survives to run."""
    monkeypatch.setattr(store_mod, "CRED_CMD_TIMEOUT_SECONDS", 1)
    marker = tmp_path / "child.marker"
    child = tmp_path / "child.py"
    child.write_text(
        f"import time, pathlib\ntime.sleep(4)\npathlib.Path(r'{marker}').write_text('x')\n",
        encoding="utf-8",
    )
    parent = (
        f"import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, r'{child}'])\n"
        f"time.sleep(30)\n"
    )
    store = CredentialStore()
    with pytest.raises(CredentialError, match="timed out"):
        store.resolve(_cmd_ref(parent))
    # Give the child longer than its own 4s sleep to (not) write the marker; the tree kill prevents it.
    time.sleep(5)
    assert not marker.exists(), "spawned child survived the timeout kill"
