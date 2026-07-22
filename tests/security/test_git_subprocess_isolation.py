"""A repository OpenAgent operates on must not be able to run code or read secrets (spec §10).

Every case here is a real delegation point in git: a program git will execute, named by
configuration that lives inside the repository being worked on. Before v0.1.5 these all fired,
because ``workspaces/worktree.py`` invoked git with ``{**os.environ, ...}`` and without disabling
hooks — so cloning a repository and letting an agent commit its work was enough to hand every
provider API key in the parent process to a shell script chosen by whoever wrote that repository.

The invariant is deliberately blunt, and it is asserted the same way for every vector: the payload
tries to write ``ANTHROPIC_API_KEY``'s value to a file, and **that file must not exist afterwards**
— or, where the hook is expected to run for a legitimate reason, must not contain the secret.

Two things these tests specifically do *not* assert:

* that git refuses to work. Isolation that breaks committing is not a fix; each case checks the
  underlying git operation still succeeded.
* that the user's own ``git commit`` is affected. The isolation applies only to subprocesses
  OpenAgent starts, and ``test_user_repository_config_is_not_modified`` pins that down.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from openagent.security.git_runner import GIT, GitRunner, git_environment

pytestmark = [pytest.mark.security, pytest.mark.subprocess]

#: The canary. If this string reaches any file written by a git-spawned child, isolation failed.
SECRET = "sk-ant-CANARY-do-not-leak-3f9a2b"

SECRET_ENV = {
    "ANTHROPIC_API_KEY": SECRET,
    "OPENAI_API_KEY": SECRET,
    "AWS_SECRET_ACCESS_KEY": SECRET,
    "GITHUB_TOKEN": SECRET,
}


requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed on this runner"
)
posix_only = pytest.mark.skipif(
    os.name != "posix", reason="shell-script hooks and 0o755 are POSIX-specific"
)


def _init_repo(root: Path) -> None:
    """A real repository with one commit, created without OpenAgent's isolation in the way."""

    root.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "-A"],
        ["commit", "-q", "-m", "initial", "--allow-empty"],
    ):
        subprocess.run(
            ["git", *args], cwd=root, env=env, check=True, capture_output=True, text=True
        )


def _install_hook(root: Path, name: str, target: Path) -> None:
    """Write an executable hook that exfiltrates the environment to ``target``."""

    hooks = root / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / name
    hook.write_text(
        "#!/bin/sh\n"
        f'printf "%s" "${{ANTHROPIC_API_KEY}}${{OPENAI_API_KEY}}" > "{target}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository with the parent process holding secrets in its environment."""

    for key, value in SECRET_ENV.items():
        monkeypatch.setenv(key, value)
    root = tmp_path / "project"
    _init_repo(root)
    (root / "file.txt").write_text("content\n", encoding="utf-8")
    return root


# --------------------------------------------------------------------------- environment


def test_git_child_receives_no_api_keys() -> None:
    """The constructed environment carries no secret-shaped variable from the parent.

    Asserted on the shape rather than on a fixed denylist: the guarantee comes from
    ``minimal_environment`` being an *allowlist*, so a provider whose variable is named something
    nobody anticipated is covered too.
    """

    env = git_environment()

    leaked = [
        key
        for key in env
        if any(token in key.upper() for token in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
    ]
    assert leaked == [], f"secret-shaped variables reached the git environment: {leaked}"
    assert SECRET not in "".join(env.values())


def test_git_child_keeps_variables_git_actually_needs() -> None:
    """Isolation that strips PATH would just break git; the allowlist is not empty."""

    env = git_environment()

    assert "PATH" in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    # Unset would mean "use whatever is configured"; empty means "there is no askpass".
    assert env["GIT_ASKPASS"] == ""
    assert env["SSH_ASKPASS"] == ""


@requires_git
def test_secret_env_does_not_reach_a_real_git_child(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: run real git and have it report the environment it was actually given.

    ``git var -l`` is not suitable — it reports config, not environment. Instead an alias is
    defined *on the command line* that echoes the variables, which exercises the same execution
    path a hook would use.
    """

    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    sink = tmp_path / "alias-output.txt"

    result = GIT.inspect(
        [
            "-c",
            f'alias.leak=!printf "%s" "${{ANTHROPIC_API_KEY}}" > "{sink}"; true',
            "leak",
        ],
        repo,
        check=False,
    )

    assert result.returncode == 0
    assert not sink.exists() or SECRET not in sink.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- hooks


@requires_git
@posix_only
@pytest.mark.parametrize(
    "hook_name", ["pre-commit", "commit-msg", "post-commit", "prepare-commit-msg"]
)
def test_git_hook_is_not_executed_on_automated_commit(
    repo: Path, tmp_path: Path, hook_name: str
) -> None:
    """No commit-lifecycle hook runs, and the commit still succeeds.

    ``--no-verify`` alone would cover ``pre-commit`` and ``commit-msg`` but not ``post-commit``;
    ``core.hooksPath`` covers all of them. Both are applied, which is why every name in the
    parametrisation passes rather than just the two the flag knows about.
    """

    sink = tmp_path / f"{hook_name}-leak.txt"
    _install_hook(repo, hook_name, sink)

    GIT.mutate_worktree(["add", "-A"], repo)
    result = GIT.commit_agent_changes("agent changes", repo)

    assert result.returncode == 0
    assert not sink.exists(), f"{hook_name} executed despite core.hooksPath isolation"
    head = GIT.inspect(["rev-parse", "HEAD"], repo).stdout.strip()
    assert len(head) == 40, "the commit did not actually land"


@requires_git
@posix_only
def test_git_hook_is_not_executed_on_worktree_mutation(repo: Path, tmp_path: Path) -> None:
    """``post-checkout`` has no ``--no-verify`` equivalent — only ``core.hooksPath`` stops it."""

    sink = tmp_path / "post-checkout-leak.txt"
    _install_hook(repo, "post-checkout", sink)

    GIT.mutate_worktree(["checkout", "-q", "-b", "openagent-run"], repo)

    assert not sink.exists()


# --------------------------------------------------------------------------- diff delegation


@requires_git
@posix_only
def test_diff_external_driver_is_not_executed(repo: Path, tmp_path: Path) -> None:
    """``diff.external`` names a program git runs for every changed file."""

    sink = tmp_path / "external-diff-leak.txt"
    driver = tmp_path / "evil-diff.sh"
    driver.write_text(
        f'#!/bin/sh\nprintf "%s" "${{ANTHROPIC_API_KEY}}" > "{sink}"\nexit 0\n', encoding="utf-8"
    )
    driver.chmod(0o755)
    subprocess.run(
        ["git", "config", "diff.external", str(driver)], cwd=repo, check=True, capture_output=True
    )
    (repo / "file.txt").write_text("changed\n", encoding="utf-8")

    GIT.diff([], repo)

    assert not sink.exists()


@requires_git
@posix_only
def test_textconv_filter_is_not_executed(repo: Path, tmp_path: Path) -> None:
    """A textconv filter is bound through ``.gitattributes``, so clearing ``diff.external`` misses it.

    This is the case that justifies passing ``--no-textconv`` as well as the config override — the
    binding lives in a tracked file in the repository, not in config.
    """

    sink = tmp_path / "textconv-leak.txt"
    filter_script = tmp_path / "evil-textconv.sh"
    filter_script.write_text(
        f'#!/bin/sh\nprintf "%s" "${{ANTHROPIC_API_KEY}}" > "{sink}"\ncat "$1"\n', encoding="utf-8"
    )
    filter_script.chmod(0o755)
    (repo / ".gitattributes").write_text("*.txt diff=evil\n", encoding="utf-8")
    subprocess.run(
        ["git", "config", "diff.evil.textconv", str(filter_script)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "file.txt").write_text("changed\n", encoding="utf-8")

    GIT.diff([], repo)

    assert not sink.exists()


@requires_git
@posix_only
def test_pager_is_not_executed(repo: Path, tmp_path: Path) -> None:
    """``core.pager`` runs for output-producing commands."""

    sink = tmp_path / "pager-leak.txt"
    pager = tmp_path / "evil-pager.sh"
    pager.write_text(
        f'#!/bin/sh\nprintf "%s" "${{ANTHROPIC_API_KEY}}" > "{sink}"\ncat\n', encoding="utf-8"
    )
    pager.chmod(0o755)
    subprocess.run(
        ["git", "config", "core.pager", str(pager)], cwd=repo, check=True, capture_output=True
    )

    GIT.inspect(["log", "--oneline"], repo)

    assert not sink.exists()


@requires_git
@posix_only
def test_credential_helper_is_not_executed(repo: Path, tmp_path: Path) -> None:
    """A credential helper is an executable named by repository config."""

    sink = tmp_path / "helper-leak.txt"
    helper = tmp_path / "evil-helper.sh"
    helper.write_text(
        f'#!/bin/sh\nprintf "%s" "${{ANTHROPIC_API_KEY}}" > "{sink}"\nexit 0\n', encoding="utf-8"
    )
    helper.chmod(0o755)
    subprocess.run(
        ["git", "config", "credential.helper", str(helper)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    # A fetch from a nonexistent remote is enough to make git reach for credentials.
    GIT.inspect(["fetch", "https://invalid.invalid/repo.git"], repo, check=False, timeout=15)

    assert not sink.exists()


# --------------------------------------------------------------------------- scope limits


@requires_git
@posix_only
def test_user_repository_config_is_not_modified(repo: Path, tmp_path: Path) -> None:
    """Isolation is per-invocation. The user's own hooks and config survive untouched.

    This is the counterweight to every test above: OpenAgent suppresses this machinery for its own
    subprocesses, and a user who runs ``git commit`` themselves must still get their hooks. If this
    test fails, the isolation was implemented by editing the repository — which would be a bug.
    """

    sink = tmp_path / "user-hook-ran.txt"
    _install_hook(repo, "pre-commit", sink)
    subprocess.run(
        ["git", "config", "core.pager", "/usr/bin/less"], cwd=repo, check=True, capture_output=True
    )

    GIT.mutate_worktree(["add", "-A"], repo)
    GIT.commit_agent_changes("agent changes", repo)

    assert (repo / ".git" / "hooks" / "pre-commit").exists(), "OpenAgent deleted the user's hook"
    configured = subprocess.run(
        ["git", "config", "--get", "core.pager"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert configured.stdout.strip() == "/usr/bin/less", "OpenAgent rewrote the user's config"

    # And the user's own commit still triggers their hook.
    (repo / "file.txt").write_text("user edit\n", encoding="utf-8")
    user_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    subprocess.run(["git", "add", "-A"], cwd=repo, env=user_env, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "user commit"],
        cwd=repo,
        env=user_env,
        check=True,
        capture_output=True,
    )
    assert sink.exists(), "the user's own hook was suppressed, which is out of scope"


# --------------------------------------------------------------------------- resource bounds


@requires_git
def test_timeout_terminates_the_process_tree(repo: Path) -> None:
    """A hung git call fails as a timeout rather than hanging the run.

    ``worktree._git`` previously used a plain ``subprocess.run``, whose timeout kills the direct
    child only; a git helper left behind keeps holding ``index.lock``. Routing through
    ``run_capture`` makes the process-group termination its docstring always claimed.
    """

    runner = GitRunner(timeout=1)

    with pytest.raises(Exception) as excinfo:
        runner.inspect(["-c", "alias.sleep=!sleep 30", "sleep"], repo, check=False)

    assert "timed out" in str(excinfo.value).lower()


# --------------------------------------------------------------------------- content filters


def _install_evil_filter(root: Path, marker: Path) -> None:
    """A repo that binds *.txt to a ``filter`` whose clean/smudge/process all run a script that
    exfiltrates the environment. ``required=true`` means git would normally *fail* the operation
    rather than skip the filter — so neutralisation has to make it a no-op, not merely optional."""

    (root / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    script = root / "evil-filter.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "RAN:%s" "${{ANTHROPIC_API_KEY}}${{OPENAI_API_KEY}}" > "{marker}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    config = root / ".git" / "config"
    with config.open("a", encoding="utf-8") as handle:
        handle.write(
            '[filter "evil"]\n'
            f"\tclean = {script}\n"
            f"\tsmudge = {script}\n"
            f"\tprocess = {script}\n"
            "\trequired = true\n"
        )


@requires_git
@posix_only
def test_content_filter_clean_does_not_run_on_git_add(repo: Path) -> None:
    """``git add`` cleans the working-tree copy through the configured filter. A repository-supplied
    filter must not run, and the add must still succeed despite ``required=true`` (spec §12)."""

    marker = repo / "filter-ran.txt"
    _install_evil_filter(repo, marker)
    (repo / "payload.txt").write_text("some content\n", encoding="utf-8")

    result = GIT.mutate_worktree(["add", "-A"], repo, check=False)

    assert not marker.exists(), "a repository content filter ran during git add"
    assert result.returncode == 0, "neutralised required filter must not fail the add"


@requires_git
@posix_only
def test_content_filter_smudge_does_not_run_on_checkout(repo: Path) -> None:
    """``git checkout`` smudges the checked-out copy through the filter; it too must be neutralised."""

    marker = repo / "filter-ran.txt"
    (repo / "tracked.txt").write_text("v1\n", encoding="utf-8")
    # Commit the file *before* the filter exists, so the checkout below is the only filter trigger.
    user_env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, env=user_env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add tracked"], cwd=repo, env=user_env, check=True)
    _install_evil_filter(repo, marker)
    (repo / "tracked.txt").unlink()

    result = GIT.mutate_worktree(["checkout", "--", "tracked.txt"], repo, check=False)

    assert not marker.exists(), "a repository content filter ran during checkout"
    assert result.returncode == 0
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "v1\n"


@requires_git
def test_discover_filter_names_finds_attribute_and_config_bindings(repo: Path) -> None:
    from openagent.security.git_runner import _discover_filter_names

    _install_evil_filter(repo, repo / "unused-marker")
    names = _discover_filter_names(repo)
    assert "evil" in names
