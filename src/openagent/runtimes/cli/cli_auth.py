"""Deciding whether a coding CLI can authenticate, and what its child process is given.

Before v0.1.5 both questions were answered by looking for a file. ``claude.py`` checked whether
``~/.claude/.credentials.json`` existed; ``codex.py`` checked ``~/.codex/auth.json``. A user who had
exported ``ANTHROPIC_API_KEY`` — the documented, supported way to authenticate — was told they were
not signed in, and the run was blocked by a mandatory preflight check.

The second half of the same bug: ``CliRunRequest.credential_env`` existed and every adapter passed
it to ``minimal_environment()``, but **nothing ever populated it**. Child processes were started
with no credentials at all, so even when OpenAgent believed a CLI was authenticated, the only
reason a run worked was that the CLI re-read its own login file from disk.

The rule this module follows is that **OpenAgent does not invent an authentication precedence.**
Each CLI already has one, it is documented, and it changes without asking us. So where a CLI can
report its own state machine-readably, that report is the authority; where it cannot, the available
evidence is reported honestly as evidence rather than being collapsed into a confident yes/no.

That distinction is not academic — the two CLIs behave differently, and pretending otherwise is how
the original bug happened:

* ``claude auth status`` emits JSON (``loggedIn``, ``authMethod``, ``apiKeySource``) and **reflects
  the environment it is given**. Run it with the same environment the child will get and its answer
  is authoritative, including which variable it chose. Note that it exits ``0`` whether or not the
  user is signed in, so the exit code says nothing; the ``loggedIn`` field is the signal.
* ``codex login status`` emits prose and reports only the *stored* login. Setting
  ``OPENAI_API_KEY`` does not change its answer. So it can prove a login exists but cannot rule one
  out, and env-var evidence has to be collected separately and reported alongside it.

Secrets are handled by name wherever possible. :class:`CliAuthEvidence` carries variable *names*,
never values, and :class:`ChildEnvironmentPlan` keeps resolved values out of its ``repr`` so a
plan that reaches a log or a traceback does not take the key with it.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from ...security.process import OutputLimitExceeded, minimal_environment, run_capture

#: Auth probes must not be able to stall a run. A CLI that hangs on a status query is a CLI that
#: reports "unknown", not one that blocks preflight forever.
AUTH_PROBE_TIMEOUT_SECONDS = 15
AUTH_PROBE_MAX_OUTPUT_BYTES = 256 * 1024


class CliCredentialSource(str, Enum):
    """Where a CLI's credential came from, as the CLI itself reports it where possible."""

    CLI_LOGIN = "cli-login"
    ENV_API_KEY = "env-api-key"
    ENV_OAUTH_TOKEN = "env-oauth-token"
    KEYCHAIN = "keychain"
    EXTERNAL_COMMAND = "external-command"
    NONE = "none"
    #: The probe ran but its answer could not be interpreted. Deliberately distinct from ``NONE``:
    #: "we could not tell" and "there is no credential" call for different messages, and conflating
    #: them is what turns a diagnostic problem into a blocked run.
    UNKNOWN = "unknown"


#: Variables that carry an actual secret. These are the ones that must be redacted, must never be
#: persisted, and are counted as evidence of an environment-provided credential.
_CLAUDE_SECRET_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)

#: Variables that configure *which* endpoint or model is used. Not secrets, but they must reach the
#: child or a user pointing at a gateway silently gets the public API instead.
_CLAUDE_CONFIG_VARS: tuple[str, ...] = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_CUSTOM_MODEL_OPTION",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION",
)

_CODEX_SECRET_VARS: tuple[str, ...] = ("OPENAI_API_KEY",)
_CODEX_CONFIG_VARS: tuple[str, ...] = ("OPENAI_BASE_URL",)

#: ``CODEX_API_KEY`` is not a variable the Codex CLI documents or reads. It appeared in an earlier
#: OpenAgent error message, so users were told to set it — and setting it did nothing. It is
#: honoured as a deprecated alias (mapped onto the real variable) rather than silently ignored,
#: because silently ignoring it reproduces the original failure with a different cause.
_DEPRECATED_ALIASES: dict[str, tuple[str, str]] = {
    "CODEX_API_KEY": ("codex", "OPENAI_API_KEY"),
}

_SECRET_VARS: dict[str, tuple[str, ...]] = {
    "claude": _CLAUDE_SECRET_VARS,
    "codex": _CODEX_SECRET_VARS,
}
_CONFIG_VARS: dict[str, tuple[str, ...]] = {
    "claude": _CLAUDE_CONFIG_VARS,
    "codex": _CODEX_CONFIG_VARS,
}

#: Variables whose presence indicates an OAuth-style token rather than an API key. Only used to
#: label the source when the CLI does not tell us itself.
_OAUTH_VARS = frozenset({"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"})


@dataclass
class CliAuthEvidence:
    """What is known about a CLI's ability to authenticate, and how it was learned.

    ``authenticated`` is deliberately three-valued. ``None`` means the probe could not determine an
    answer — a CLI with no status surface, a timeout, unparseable output. Collapsing that into
    ``False`` is what blocked runs for correctly-configured users, so callers are made to handle it.
    """

    cli_type: str
    authenticated: bool | None
    source: CliCredentialSource
    detail: str
    executable: str = ""
    #: Names only. A value never enters this structure.
    environment_names: list[str] = field(default_factory=list)
    #: Human-readable notes about credentials that disagree with each other.
    conflicts: list[str] = field(default_factory=list)
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def blocking(self) -> bool:
        """Whether this should stop a run.

        Only a definite negative blocks. "Unknown" lets the run proceed and fail with the CLI's own
        error message, which is more informative than OpenAgent's guess about why it might fail.
        """

        return self.authenticated is False


@dataclass
class ChildEnvironmentPlan:
    """Exactly what a CLI child process will receive.

    Split so the public half can be logged and shown in diagnostics while the secret half cannot.
    ``secret_env`` is excluded from ``repr`` — a dataclass repr is precisely the thing that ends up
    in an exception message.
    """

    cli_type: str
    public_env: dict[str, str] = field(default_factory=dict)
    secret_env: dict[str, str] = field(default_factory=dict, repr=False)
    auth_source: CliCredentialSource = CliCredentialSource.NONE
    #: Deprecation notices to surface through doctor, e.g. a CODEX_API_KEY that was remapped.
    notices: list[str] = field(default_factory=list)

    @property
    def secret_env_names(self) -> list[str]:
        return sorted(self.secret_env)

    def as_child_env(self) -> dict[str, str]:
        """The mapping to hand to ``minimal_environment(extra=...)``."""

        return {**self.public_env, **self.secret_env}

    def secret_values(self) -> list[str]:
        """Values to register for output redaction. Never persisted, never logged."""

        return [value for value in self.secret_env.values() if value]


def build_child_environment(
    cli_type: str, environ: Mapping[str, str] | None = None
) -> ChildEnvironmentPlan:
    """Collect the credential and endpoint variables ``cli_type`` is documented to read.

    An allowlist per CLI, not a pass-through. A Claude run must not receive ``OPENAI_API_KEY`` just
    because the user has one exported — the child gets the credentials for the service it is
    actually talking to and nothing else.
    """

    env = os.environ if environ is None else environ
    plan = ChildEnvironmentPlan(cli_type=cli_type)

    for name in _CONFIG_VARS.get(cli_type, ()):
        value = env.get(name)
        if value:
            plan.public_env[name] = value

    for name in _SECRET_VARS.get(cli_type, ()):
        value = env.get(name)
        if value:
            plan.secret_env[name] = value

    for alias, (owner, canonical) in _DEPRECATED_ALIASES.items():
        if owner != cli_type:
            continue
        value = env.get(alias)
        if value and canonical not in plan.secret_env:
            plan.secret_env[canonical] = value
            plan.notices.append(
                f"{alias} is not read by the {cli_type} CLI and is deprecated in OpenAgent; "
                f"its value was forwarded as {canonical}. Set {canonical} directly."
            )

    plan.auth_source = _source_for(plan.secret_env)
    return plan


def _source_for(secret_env: Mapping[str, str]) -> CliCredentialSource:
    if not secret_env:
        return CliCredentialSource.NONE
    if any(name in _OAUTH_VARS for name in secret_env):
        return CliCredentialSource.ENV_OAUTH_TOKEN
    return CliCredentialSource.ENV_API_KEY


def describe_conflicts(plan: ChildEnvironmentPlan) -> list[str]:
    """Note when several credentials are present at once.

    This is a warning, never an error. The CLI picks one by its own documented precedence and that
    choice is correct by definition; the value of saying so is that "I updated my key and nothing
    changed" is otherwise very hard to diagnose.
    """

    names = plan.secret_env_names
    if len(names) < 2:
        return []
    return [
        "multiple credentials are set for this CLI ("
        + ", ".join(names)
        + "); the CLI chooses one by its own precedence"
    ]


# --------------------------------------------------------------------------- probes


def _probe(argv: list[str], env: Mapping[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return run_capture(
        argv,
        cwd=cwd,
        env=dict(env),
        timeout=AUTH_PROBE_TIMEOUT_SECONDS,
        shell=False,
        max_output_bytes=AUTH_PROBE_MAX_OUTPUT_BYTES,
    )


def probe_claude_auth(
    executable: str,
    plan: ChildEnvironmentPlan,
    *,
    cwd: Path | None = None,
) -> CliAuthEvidence:
    """Ask Claude Code itself, with the environment the child will actually get.

    Running the probe under a *different* environment than the run would be worse than not probing:
    it would answer a question nobody asked. The whole point is that ``claude auth status``
    resolves the same precedence the real run will.

    Its JSON reports ``loggedIn``, ``authMethod`` (``api_key`` / ``oauth_token`` / ``none``) and,
    for API keys, ``apiKeySource`` — the variable it actually chose. That last field is what makes
    a conflicting-credentials warning actionable rather than vague.
    """

    env = minimal_environment(plan.as_child_env())
    directory = cwd or Path.home()
    names = plan.secret_env_names

    try:
        result = _probe([executable, "auth", "status"], env, directory)
    except (OSError, subprocess.TimeoutExpired, OutputLimitExceeded) as exc:
        return _claude_fallback(
            plan,
            executable,
            names,
            detail=f"`claude auth status` could not be run ({type(exc).__name__}); "
            "falling back to environment and credential-file evidence",
        )

    payload: object = None
    try:
        payload = json.loads(result.stdout or "")
    except ValueError:
        payload = None

    if not isinstance(payload, dict):
        return _claude_fallback(
            plan,
            executable,
            names,
            detail="`claude auth status` produced no parseable JSON; "
            "falling back to environment and credential-file evidence",
        )

    logged_in = payload.get("loggedIn")
    method = str(payload.get("authMethod") or "")
    key_source = str(payload.get("apiKeySource") or "")
    provider = str(payload.get("apiProvider") or "")

    if method == "oauth_token":
        source = CliCredentialSource.ENV_OAUTH_TOKEN if names else CliCredentialSource.CLI_LOGIN
    elif method == "api_key":
        source = CliCredentialSource.ENV_API_KEY if key_source else CliCredentialSource.CLI_LOGIN
    elif logged_in:
        source = CliCredentialSource.CLI_LOGIN
    else:
        source = CliCredentialSource.NONE

    if logged_in is True:
        via = f" via {key_source}" if key_source else ""
        detail = f"claude auth status: signed in ({method or 'unknown method'}{via})"
        if provider and provider != "firstParty":
            detail += f", provider {provider}"
    elif logged_in is False:
        detail = (
            "claude auth status: not signed in. Run `claude auth login`, "
            "or set ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN"
        )
    else:
        detail = "claude auth status did not report a login state"

    conflicts = describe_conflicts(plan)
    if key_source and len(names) > 1:
        conflicts = [
            f"several credentials are set ({', '.join(names)}); Claude Code is using {key_source}"
        ]

    return CliAuthEvidence(
        cli_type="claude",
        authenticated=logged_in if isinstance(logged_in, bool) else None,
        source=source,
        detail=detail,
        executable=executable,
        environment_names=names,
        conflicts=conflicts,
    )


def _claude_fallback(
    plan: ChildEnvironmentPlan,
    executable: str,
    names: list[str],
    *,
    detail: str,
) -> CliAuthEvidence:
    """Evidence when the CLI's own status surface is unavailable.

    An exported credential is treated as authenticated here. It is what the CLI documents itself as
    reading, and the alternative — refusing to start because we could not confirm it — is exactly
    the behavior being fixed.
    """

    if names:
        return CliAuthEvidence(
            cli_type="claude",
            authenticated=True,
            source=plan.auth_source,
            detail=f"{detail}; credential present in {', '.join(names)}",
            executable=executable,
            environment_names=names,
            conflicts=describe_conflicts(plan),
        )

    credentials_file = Path.home() / ".claude" / ".credentials.json"
    legacy_file = Path.home() / ".claude.json"
    if credentials_file.exists() or legacy_file.exists():
        return CliAuthEvidence(
            cli_type="claude",
            authenticated=True,
            source=CliCredentialSource.CLI_LOGIN,
            detail=f"{detail}; stored credentials present under ~/.claude",
            executable=executable,
            environment_names=[],
        )
    return CliAuthEvidence(
        cli_type="claude",
        authenticated=False,
        source=CliCredentialSource.NONE,
        detail=(
            "no Claude credentials found. Run `claude auth login`, "
            "or set ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN"
        ),
        executable=executable,
        environment_names=[],
    )


def probe_codex_auth(
    executable: str,
    plan: ChildEnvironmentPlan,
    *,
    cwd: Path | None = None,
) -> CliAuthEvidence:
    """Collect Codex evidence from two independent sources, because neither one is sufficient.

    ``codex login status`` reports the *stored* login only: it prints "Logged in using ChatGPT"
    whether or not ``OPENAI_API_KEY`` is set, and exits ``0`` either way. So it can establish that
    a login exists but cannot establish that one does not — an exported API key is invisible to it.

    Treating its output as the whole answer would reproduce the original bug in mirror image. Both
    signals are gathered and the positive one wins; the CLI resolves its own precedence at run time.
    """

    env = minimal_environment(plan.as_child_env())
    directory = cwd or Path.home()
    names = plan.secret_env_names

    stored_login: bool | None = None
    login_detail = ""
    try:
        result = _probe([executable, "login", "status"], env, directory)
        text = (result.stdout or result.stderr or "").strip()
        lowered = text.lower()
        if "not logged in" in lowered or "logged out" in lowered or "no credentials" in lowered:
            stored_login = False
        elif "logged in" in lowered:
            stored_login = True
            login_detail = text.splitlines()[0][:200]
        else:
            stored_login = None
    except (OSError, subprocess.TimeoutExpired, OutputLimitExceeded):
        stored_login = None

    auth_file = Path.home() / ".codex" / "auth.json"
    if stored_login is None and auth_file.exists():
        stored_login = True
        login_detail = "~/.codex/auth.json present"

    conflicts = describe_conflicts(plan)
    if names and stored_login:
        conflicts.append(
            f"both a stored Codex login and {', '.join(names)} are present; "
            "the CLI chooses one by its own precedence"
        )

    if names:
        detail = f"credential present in {', '.join(names)}"
        if login_detail:
            detail += f"; {login_detail}"
        return CliAuthEvidence(
            cli_type="codex",
            authenticated=True,
            source=plan.auth_source,
            detail=detail,
            executable=executable,
            environment_names=names,
            conflicts=conflicts,
        )

    if stored_login:
        return CliAuthEvidence(
            cli_type="codex",
            authenticated=True,
            source=CliCredentialSource.CLI_LOGIN,
            detail=login_detail or "codex login status: signed in",
            executable=executable,
            environment_names=[],
        )

    if stored_login is False:
        return CliAuthEvidence(
            cli_type="codex",
            authenticated=False,
            source=CliCredentialSource.NONE,
            detail="codex login status: not signed in. Run `codex login`, or set OPENAI_API_KEY",
            executable=executable,
            environment_names=[],
        )

    return CliAuthEvidence(
        cli_type="codex",
        authenticated=None,
        source=CliCredentialSource.UNKNOWN,
        detail=(
            "codex login status gave no usable answer and no OPENAI_API_KEY is set; "
            "the run will surface the CLI's own error if it cannot authenticate"
        ),
        executable=executable,
        environment_names=[],
    )
