"""Private files mean different things on POSIX and Windows, and both are asserted.

This suite exists because the previous enforcement asserted ``stat.S_IMODE(...) == 0o600``
everywhere. On Windows that is not a weaker claim, it is a claim about a field the OS does not
use: ``os.open(..., 0o600)`` there leaves the mode reading ``0o666`` and the real access control —
the DACL — entirely inherited from the parent directory. The test failed on CI, and the tempting
"fix" (accept ``0o666`` too) would have left a security control with no test at all.

So the contract is stated once, in :mod:`openagent.security.private_files`, and asserted twice:
POSIX bits under ``skipif(nt)``, Windows ACLs under ``skipif(not nt)``. The platform-independent
tests below hold on both.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from openagent.security.private_files import (
    DIRECTORY_MODE,
    FILE_MODE,
    PrivateFileError,
    create_private_directory,
    create_private_file,
    private_directory,
    verify_private_directory,
    verify_private_file,
)

WINDOWS = os.name == "nt"
posix_only = pytest.mark.skipif(WINDOWS, reason="POSIX permission bits")
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows DACL")


# ------------------------------------------------------------------ platform independent


def test_a_created_file_verifies_as_private(tmp_path: Path) -> None:
    with private_directory("openagent-test-") as directory:
        path = directory / "secret.json"
        create_private_file(path, b'{"policy": true}')
        assert verify_private_file(path).ok
        assert path.read_bytes() == b'{"policy": true}'


def test_a_created_directory_verifies_as_private() -> None:
    with private_directory("openagent-test-") as directory:
        verification = verify_private_directory(directory)
        assert verification.ok, verification.findings


def test_create_private_directory_at_a_caller_chosen_path(tmp_path: Path) -> None:
    """The primitive is usable outside the temp-directory helper, and still verifies."""

    target = tmp_path / "policy-root"
    create_private_directory(target)
    assert verify_private_directory(target).ok


def test_create_private_directory_refuses_an_existing_path(tmp_path: Path) -> None:
    target = tmp_path / "policy-root"
    create_private_directory(target)
    with pytest.raises(OSError):
        create_private_directory(target)


def test_the_directory_is_removed_on_exit() -> None:
    with private_directory("openagent-test-") as directory:
        (directory / "f").write_text("x")
        remembered = directory
    assert not remembered.exists()


class _Boom(Exception):
    """Distinct from PrivateFileError, which is a RuntimeError.

    Raising RuntimeError here would let a *failure to create* the directory satisfy the
    pytest.raises and leave the real assertion untested — which is exactly what happened on the
    first Windows run of this suite.
    """


def test_the_directory_is_removed_even_when_the_body_raises() -> None:
    remembered: Path | None = None
    with pytest.raises(_Boom):
        with private_directory("openagent-test-") as directory:
            remembered = directory
            raise _Boom("boom")
    assert remembered is not None
    assert not remembered.exists()


def test_creating_over_an_existing_file_fails(tmp_path: Path) -> None:
    """Never truncate: an existing path at that name is somebody else's, not ours to reuse."""

    with private_directory("openagent-test-") as directory:
        path = directory / "secret.json"
        create_private_file(path, b"first")
        with pytest.raises(OSError):
            create_private_file(path, b"second")
        assert path.read_bytes() == b"first"


def test_verification_reports_a_missing_path_rather_than_raising(tmp_path: Path) -> None:
    verification = verify_private_file(tmp_path / "absent")
    assert not verification.ok
    assert verification.findings


def test_verification_of_a_directory_as_a_file_fails() -> None:
    with private_directory("openagent-test-") as directory:
        assert not verify_private_file(directory).ok


def test_the_backend_name_says_which_contract_was_checked() -> None:
    with private_directory("openagent-test-") as directory:
        expected = "windows-dacl" if WINDOWS else "posix-mode"
        assert verify_private_directory(directory).backend == expected


def test_raise_if_insecure_names_the_path_and_the_findings(tmp_path: Path) -> None:
    verification = verify_private_file(tmp_path / "absent")
    with pytest.raises(PrivateFileError) as caught:
        verification.raise_if_insecure()
    assert "absent" in str(caught.value)


# ------------------------------------------------------------------------------ POSIX


@posix_only
def test_posix_file_mode_is_exactly_0600() -> None:
    with private_directory("openagent-test-") as directory:
        path = directory / "secret.json"
        create_private_file(path, b"x")
        assert stat.S_IMODE(path.stat().st_mode) == FILE_MODE


@posix_only
def test_posix_directory_mode_is_exactly_0700() -> None:
    with private_directory("openagent-test-") as directory:
        assert stat.S_IMODE(directory.stat().st_mode) == DIRECTORY_MODE


@posix_only
def test_posix_grants_nothing_to_group_or_other() -> None:
    with private_directory("openagent-test-") as directory:
        path = directory / "secret.json"
        create_private_file(path, b"x")
        for target in (path, directory):
            mode = stat.S_IMODE(target.stat().st_mode)
            assert not mode & stat.S_IRWXG, f"{target} grants group access"
            assert not mode & stat.S_IRWXO, f"{target} grants other access"


@posix_only
def test_posix_a_permissive_umask_cannot_widen_the_file() -> None:
    """``os.open``'s mode argument is masked by the umask; ``fchmod`` afterwards is not."""

    previous = os.umask(0o000)
    try:
        with private_directory("openagent-test-") as directory:
            path = directory / "secret.json"
            create_private_file(path, b"x")
            assert stat.S_IMODE(path.stat().st_mode) == FILE_MODE
            assert stat.S_IMODE(directory.stat().st_mode) == DIRECTORY_MODE
    finally:
        os.umask(previous)


@posix_only
def test_posix_a_restrictive_umask_cannot_narrow_the_directory() -> None:
    """A umask that clears owner bits would otherwise leave a directory we cannot enter."""

    previous = os.umask(0o700)
    try:
        with private_directory("openagent-test-") as directory:
            assert stat.S_IMODE(directory.stat().st_mode) == DIRECTORY_MODE
    finally:
        os.umask(previous)


@posix_only
def test_posix_an_ordinarily_created_file_fails_verification() -> None:
    """The verifier is not a formality: hand it a normal file and it must refuse.

    The negative control deliberately does *not* ``chmod`` to a permissive mask. It writes the file
    the ordinary way, under an ordinary ``umask``, which is exactly how this mistake happens in
    real code — someone reaches for ``Path.write_text`` instead of ``create_private_file`` and gets
    ``0644`` without ever choosing it. Asserting the verifier rejects that is a stronger claim than
    asserting it rejects a mask nobody would write on purpose.

    (It also keeps the suite free of a real world-readable ``chmod``, which a static analyser is
    right to flag and which no amount of "but it is only a test" makes safe to normalise.)
    """

    previous = os.umask(0o022)
    try:
        with private_directory("openagent-test-") as directory:
            path = directory / "secret.json"
            path.write_text("x")
            assert stat.S_IMODE(path.stat().st_mode) == 0o644, "umask did not apply as expected"
            verification = verify_private_file(path)
            assert not verification.ok
            assert any("mode is" in finding for finding in verification.findings)
            assert any("other has access" in finding for finding in verification.findings)
            assert any("group has access" in finding for finding in verification.findings)
    finally:
        os.umask(previous)


@posix_only
def test_posix_a_symlink_is_refused_by_verification(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("x")
    os.chmod(target, 0o600)
    link = tmp_path / "link"
    link.symlink_to(target)
    verification = verify_private_file(link)
    assert not verification.ok
    assert any("symlink" in finding for finding in verification.findings)


@posix_only
def test_posix_creation_refuses_to_follow_a_symlink(tmp_path: Path) -> None:
    """The classic local attack: pre-place a symlink where the policy file is about to land."""

    victim = tmp_path / "victim"
    victim.write_text("original")
    with private_directory("openagent-test-") as directory:
        attack = directory / "settings.json"
        attack.symlink_to(victim)
        with pytest.raises(OSError):
            create_private_file(attack, b"attacker-chosen policy")
    assert victim.read_text() == "original"


# ---------------------------------------------------------------------------- Windows


@windows_only
def test_windows_owner_is_the_current_user() -> None:
    with private_directory("openagent-test-") as directory:
        path = directory / "secret.json"
        create_private_file(path, b"x")
        verification = verify_private_file(path)
        assert verification.ok, verification.findings
        assert verification.owner and verification.owner.startswith("S-1-")


@windows_only
def test_windows_inheritance_is_disabled_on_file_and_directory() -> None:
    """A protected DACL is what stops the parent's ACEs from being merged back in."""

    with private_directory("openagent-test-") as directory:
        path = directory / "secret.json"
        create_private_file(path, b"x")
        assert verify_private_file(path).inheritance_disabled is True
        assert verify_private_directory(directory).inheritance_disabled is True


@windows_only
def test_windows_no_broad_group_holds_an_ace() -> None:
    """Everyone / Users / Authenticated Users are the three that would make it world-readable."""

    with private_directory("openagent-test-") as directory:
        path = directory / "secret.json"
        create_private_file(path, b"x")
        verification = verify_private_file(path)
        assert verification.ok, verification.findings
        rendered = _icacls(path)
        for group in (
            "Everyone",
            "BUILTIN\\Users",
            "Authenticated Users",
            "NT AUTHORITY\\INTERACTIVE",
        ):
            assert group not in rendered, f"{group} appears in the ACL:\n{rendered}"


@windows_only
def test_windows_the_current_user_can_still_read_and_delete() -> None:
    """Locking everyone out including ourselves would be a different bug with the same test."""

    with private_directory("openagent-test-") as directory:
        path = directory / "secret.json"
        create_private_file(path, b"payload")
        assert path.read_bytes() == b"payload"
        path.unlink()
        assert not path.exists()


@windows_only
def test_windows_verification_rejects_an_ace_we_did_not_write() -> None:
    """Independently widen the ACL with icacls; the verifier must fail it.

    ``icacls`` appears here and nowhere in the runtime: a security check that depends on a
    subprocess inherits that subprocess's PATH resolution. As a *second opinion in a test* it is
    exactly right, because it is not the code under test.
    """

    with private_directory("openagent-test-") as directory:
        path = directory / "secret.json"
        create_private_file(path, b"x")
        assert verify_private_file(path).ok
        subprocess.run(
            ["icacls", str(path), "/grant", "*S-1-5-32-545:(R)"],  # BUILTIN\Users
            check=True,
            capture_output=True,
        )
        verification = verify_private_file(path)
        assert not verification.ok, "a Users ACE was added and verification still passed"
        assert any(
            "Users" in finding or "unexpected trustee" in finding
            for finding in verification.findings
        )


@windows_only
def test_windows_a_readable_file_is_not_assumed_from_the_mode() -> None:
    """Documents the platform fact this module exists for, so it cannot regress silently."""

    with private_directory("openagent-test-") as directory:
        path = directory / "secret.json"
        create_private_file(path, b"x")
        # Windows reports 0666 for a perfectly private file. Any future test asserting 0600 here
        # is asserting something the OS never promised.
        assert stat.S_IMODE(path.stat().st_mode) in (0o666, 0o444)
        assert verify_private_file(path).ok


def _icacls(path: Path) -> str:
    result = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=False)
    return result.stdout + result.stderr
