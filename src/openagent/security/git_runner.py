"""The single entry point for every git subprocess OpenAgent starts.

Git is not a pure function of its arguments. It reads three levels of configuration, executes hooks
from the repository it is operating on, and will happily shell out to a pager, a credential helper,
an askpass program, an external diff driver or a textconv filter — all of them named by
configuration that lives **inside the repository the agent is working in**.

That matters here because OpenAgent runs git against repositories it did not write. Before this
module, ``workspaces/worktree.py`` invoked git with ``{**os.environ, ...}``: every variable in the
parent process, including every provider API key, was handed to a child that a checked-out
``.git/hooks/pre-commit`` could take over. Committing the agent's work was enough to trigger it.

So the rule this module enforces is narrow and absolute: **a git subprocess OpenAgent starts gets
no secrets and runs no repository-supplied code.**

Three things are worth stating plainly, because each is a place the obvious implementation gets it
wrong:

* *Hooks are disabled by pointing ``core.hooksPath`` at an empty directory, not by trusting
  ``--no-verify``.* ``--no-verify`` only covers a handful of commit-time hooks; it does nothing for
  ``post-checkout``, ``post-merge`` or ``reference-transaction``. Both are used — the flag for the
  hooks it does cover, the empty directory for everything else.

* *Configuration is neutralised per-invocation with ``-c``, not by editing anything.* The
  repository's own ``.git/config`` is left exactly as the user wrote it. ``-c`` overrides win over
  file-level configuration for the life of one process, which is the entire scope of the guarantee
  being made.

* *This applies only to git that OpenAgent starts.* A user running ``git commit`` in their own
  terminal gets their hooks, their pager, their signing key, and their identity. Disabling a user's
  own tooling would be a bug, not a hardening measure.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .process import OutputLimitExceeded, minimal_environment, run_capture

IS_WINDOWS = sys.platform.startswith("win")

#: Every git call is bounded. git can block indefinitely — an ``index.lock`` held by another
#: process, a network remote, a credential prompt that ``GIT_TERMINAL_PROMPT`` did not catch — and
#: an unbounded call hangs the whole run with no diagnosis.
GIT_TIMEOUT = 60

#: Diffs of a large working tree are the one genuinely big output here. The cap is a real memory
#: bound enforced while reading, not a check performed after the fact.
GIT_MAX_OUTPUT_BYTES = 32 * 1024 * 1024

#: Environment variables that pin git to non-interactive, non-delegating behavior.
#:
#: ``GIT_ASKPASS`` and ``SSH_ASKPASS`` are set to the empty string rather than left unset: unset
#: means "fall back to whatever is configured", and the configured value is attacker-controlled in
#: the threat model this module exists for.
_GIT_HARDENING_ENV: dict[str, str] = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "GIT_PAGER": "cat",
    "GIT_OPTIONAL_LOCKS": "0",
    # Ignore /etc/gitconfig. The user's own global config is neutralised separately, per-call,
    # because doing it needs a real path (see _config_isolation_env).
    "GIT_CONFIG_NOSYSTEM": "1",
    # A commit needs an identity or git refuses outright. These are overridden by explicit -c
    # flags for the commit itself; the environment values are the floor, not the authority.
    "GIT_AUTHOR_NAME": "OpenAgent",
    "GIT_AUTHOR_EMAIL": "openagent@local",
    "GIT_COMMITTER_NAME": "OpenAgent",
    "GIT_COMMITTER_EMAIL": "openagent@local",
}

#: ``-c`` overrides applied to every invocation. These are the delegation points: each one names a
#: program git would otherwise run, chosen by repository configuration.
_GIT_SAFE_CONFIG: tuple[str, ...] = (
    "core.fsmonitor=false",  # a configured fsmonitor is an executable
    "core.pager=cat",
    "core.askpass=",
    "core.sshCommand=",
    "diff.external=",  # an external diff driver is an executable
    "credential.helper=",  # a credential helper is an executable
    "protocol.ext.allow=never",  # ext:: URLs run a shell command
    "uploadpack.packObjectsHook=",
)


class GitError(RuntimeError):
    """A git command exited non-zero."""


class GitTimeout(GitError):
    """A git command exceeded its budget; its process tree was terminated."""


class GitMissing(GitError):
    """git is not installed or not on PATH."""


@dataclass(frozen=True)
class GitResult:
    stdout: str
    stderr: str
    returncode: int


def _empty_hooks_dir() -> Path:
    """A directory that exists and contains no hooks, for ``core.hooksPath``.

    Created once per process under the system temp directory with private permissions. It must
    *exist*: git treats a missing ``hooksPath`` as "no hooks" today, but relying on that is relying
    on the absence of a directory an attacker cannot create — whereas an empty directory we own is
    a positive guarantee.
    """

    global _HOOKS_DIR
    if _HOOKS_DIR is None or not _HOOKS_DIR.is_dir():
        _HOOKS_DIR = Path(tempfile.mkdtemp(prefix="openagent-nohooks-"))
        os.chmod(_HOOKS_DIR, 0o700)
    return _HOOKS_DIR


_HOOKS_DIR: Path | None = None


def _null_config_file() -> str:
    """A path git can read as an empty configuration file.

    ``/dev/null`` is the natural answer on POSIX. Windows has no such path that git's config
    parser accepts, so an empty real file is created once per process instead.
    """

    global _NULL_CONFIG
    if not IS_WINDOWS:
        return os.devnull
    if _NULL_CONFIG is None or not Path(_NULL_CONFIG).is_file():
        handle, path = tempfile.mkstemp(prefix="openagent-nullconfig-", suffix=".ini")
        os.close(handle)
        _NULL_CONFIG = path
    return _NULL_CONFIG


_NULL_CONFIG: str | None = None


def _config_isolation_env() -> dict[str, str]:
    """Point git's global and system config at nothing.

    Without this, a ``[core] hooksPath`` or ``[diff] external`` in the *user's* ``~/.gitconfig``
    would still apply to automated calls. That is not an attack — it is the user's own machine —
    but it makes OpenAgent's behavior depend on state it does not control and cannot report on, and
    a run that succeeds on one machine and fails on another for that reason is not debuggable.
    """

    null = _null_config_file()
    return {"GIT_CONFIG_GLOBAL": null, "GIT_CONFIG_SYSTEM": null}


def git_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The complete environment for an OpenAgent-started git process.

    Built from :func:`minimal_environment`, which carries only PATH/HOME/locale-class variables and
    deliberately drops everything else — so no ``*_API_KEY``, ``*_TOKEN`` or ``*_SECRET`` from the
    parent reaches git, without needing a denylist that would have to anticipate every provider's
    naming scheme.
    """

    env = minimal_environment()
    env.update(_GIT_HARDENING_ENV)
    env.update(_config_isolation_env())
    if extra:
        env.update(extra)
    return env


def _hardened_argv(args: Sequence[str]) -> list[str]:
    argv = ["git", "--no-pager", "-c", f"core.hooksPath={_empty_hooks_dir()}"]
    for setting in _GIT_SAFE_CONFIG:
        argv += ["-c", setting]
    argv += list(args)
    return argv


class GitRunner:
    """Runs git against a working tree with hooks, helpers and secrets removed.

    The three public methods differ only in intent and in the extra flags they add; they share one
    execution path so a hardening fix cannot land on the read path and miss the write path.
    """

    def __init__(self, *, timeout: int = GIT_TIMEOUT) -> None:
        self.timeout = timeout

    # ------------------------------------------------------------------ execution

    def _run(
        self,
        args: Sequence[str],
        cwd: Path,
        *,
        timeout: int | None = None,
        check: bool = True,
    ) -> GitResult:
        argv = _hardened_argv(args)
        effective_timeout = self.timeout if timeout is None else timeout
        try:
            completed = run_capture(
                argv,
                cwd=cwd,
                env=git_environment(),
                timeout=effective_timeout,
                shell=False,
                max_output_bytes=GIT_MAX_OUTPUT_BYTES,
            )
        except FileNotFoundError as exc:
            # Callers decide what this means; is_git_repo() treats it as "not a repo" so that a
            # machine without git degrades to a copy workspace instead of failing the run.
            raise GitMissing("git is not installed or not on PATH") from exc
        except OutputLimitExceeded as exc:
            raise GitError(
                f"git {' '.join(args)} produced more than {GIT_MAX_OUTPUT_BYTES} bytes"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitTimeout(f"git {' '.join(args)} timed out after {effective_timeout}s") from exc
        result = GitResult(
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            returncode=completed.returncode,
        )
        if check and result.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result

    # ------------------------------------------------------------------ read-only

    def inspect(
        self,
        args: Sequence[str],
        cwd: Path,
        *,
        timeout: int | None = None,
        check: bool = True,
    ) -> GitResult:
        """Run a read-only query (``status``, ``rev-parse``, ``ls-files``, …)."""

        return self._run(args, cwd, timeout=timeout, check=check)

    def diff(
        self,
        args: Sequence[str],
        cwd: Path,
        *,
        timeout: int | None = None,
        check: bool = True,
    ) -> str:
        """Run a diff with content filters disabled.

        ``--no-ext-diff`` and ``--no-textconv`` are the flag-level counterparts to the
        ``diff.external=`` config override: a ``.gitattributes`` in the repository can bind a
        textconv filter to a path pattern, and that binding is not reached by clearing
        ``diff.external``.

        ``check=False`` is needed for ``--no-index``, where exit 1 means "the files differ" — the
        expected outcome, not a failure.
        """

        return self._run(
            ["diff", "--no-ext-diff", "--no-textconv", *args], cwd, timeout=timeout, check=check
        ).stdout

    # ------------------------------------------------------------------ mutating

    def mutate_worktree(
        self,
        args: Sequence[str],
        cwd: Path,
        *,
        timeout: int | None = None,
        check: bool = True,
    ) -> GitResult:
        """Run a command that changes the working tree or index (``add``, ``checkout``, ``revert``).

        Same isolation as the read path. ``post-checkout`` and ``post-merge`` hooks are covered by
        ``core.hooksPath``; there is no flag equivalent for them.
        """

        return self._run(args, cwd, timeout=timeout, check=check)

    def commit_agent_changes(
        self,
        message: str,
        cwd: Path,
        *,
        timeout: int | None = None,
    ) -> GitResult:
        """Create OpenAgent's own commit of the agent's work.

        The identity is pinned rather than inherited so the commit is attributable to OpenAgent and
        does not silently borrow the user's name — and, more practically, so the commit does not
        fail on a machine where the user never configured ``user.email``.

        Signing is disabled explicitly: ``commit.gpgSign=true`` in the user's global config would
        otherwise make every automated commit block on a passphrase prompt or an agent that is not
        running.
        """

        args = [
            "-c",
            "commit.gpgSign=false",
            "-c",
            "user.name=OpenAgent",
            "-c",
            "user.email=openagent@local",
            "commit",
            "--no-verify",
            "-m",
            message,
        ]
        return self._run(args, cwd, timeout=timeout)


#: Shared instance. GitRunner holds no per-repository state — cwd is a parameter, not a field — so
#: one instance serves every caller.
GIT = GitRunner()
