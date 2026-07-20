"""A CLI child gets the credentials for its own service, and nothing else (spec §7).

Two defects, both fixed in v0.1.5, both reproduced here.

**Authentication was decided by looking for a file.** ``claude.py`` checked whether
``~/.claude/.credentials.json`` existed and ``codex.py`` checked ``~/.codex/auth.json``. Exporting
``ANTHROPIC_API_KEY`` — documented and supported — produced "not signed in" and a *mandatory*
preflight failure, so the run never started.

**Nothing populated the child environment.** ``CliRunRequest.credential_env`` existed and every
adapter forwarded it to ``minimal_environment()``, but no code path ever filled it in. CLI children
were started with no credentials at all; runs worked only because the CLI re-read its own login
file from disk, which an env-var-based setup does not have.

The isolation half matters as much as the delivery half: a Claude run must not receive
``OPENAI_API_KEY`` merely because the user has one exported. ``test_cross_cli_credentials_are_not_
inherited`` is the case that would otherwise send an OpenAI key to Anthropic's CLI.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from openagent.runtimes.cli.cli_auth import (
    CliCredentialSource,
    build_child_environment,
    describe_conflicts,
    probe_claude_auth,
    probe_codex_auth,
)
from openagent.security.process import minimal_environment

pytestmark = [pytest.mark.security, pytest.mark.unit]

ANTHROPIC_SECRET = "sk-ant-CANARY-anthropic-9f3b"
OPENAI_SECRET = "sk-CANARY-openai-2d7c"
UNRELATED_SECRET = "AKIA-CANARY-unrelated-5e1a"


@pytest.fixture
def all_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """A parent process holding credentials for several unrelated services at once."""

    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_SECRET)
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_SECRET)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", UNRELATED_SECRET)
    monkeypatch.setenv("GITHUB_TOKEN", UNRELATED_SECRET)
    monkeypatch.setenv("STRIPE_SECRET_KEY", UNRELATED_SECRET)


# --------------------------------------------------------------------------- delivery


def test_claude_env_api_key_reaches_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_SECRET)

    plan = build_child_environment("claude")

    assert plan.as_child_env()["ANTHROPIC_API_KEY"] == ANTHROPIC_SECRET
    assert plan.auth_source is CliCredentialSource.ENV_API_KEY


def test_claude_oauth_token_is_recognised(monkeypatch: pytest.MonkeyPatch) -> None:
    """An OAuth token is a different source from an API key, and is labelled as one."""

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", ANTHROPIC_SECRET)

    plan = build_child_environment("claude")

    assert plan.as_child_env()["CLAUDE_CODE_OAUTH_TOKEN"] == ANTHROPIC_SECRET
    assert plan.auth_source is CliCredentialSource.ENV_OAUTH_TOKEN


def test_codex_openai_api_key_reaches_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_SECRET)

    plan = build_child_environment("codex")

    assert plan.as_child_env()["OPENAI_API_KEY"] == OPENAI_SECRET


def test_endpoint_configuration_reaches_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user pointed at a gateway must not silently get the public API instead."""

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example.invalid")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-4-8")

    plan = build_child_environment("claude")

    assert plan.public_env["ANTHROPIC_BASE_URL"] == "https://gateway.example.invalid"
    assert plan.public_env["ANTHROPIC_MODEL"] == "claude-opus-4-8"
    # Endpoint configuration is not a secret and must not be treated as one — redacting a base URL
    # out of the logs would make a misrouted request impossible to diagnose.
    assert "ANTHROPIC_BASE_URL" not in plan.secret_env_names


# --------------------------------------------------------------------------- isolation


def test_cross_cli_credentials_are_not_inherited(all_secrets: None) -> None:
    """The Claude child never sees the OpenAI key, and vice versa."""

    claude_env = build_child_environment("claude").as_child_env()
    codex_env = build_child_environment("codex").as_child_env()

    assert OPENAI_SECRET not in claude_env.values()
    assert "OPENAI_API_KEY" not in claude_env
    assert ANTHROPIC_SECRET not in codex_env.values()
    assert "ANTHROPIC_API_KEY" not in codex_env


def test_unrelated_secrets_are_never_inherited(all_secrets: None) -> None:
    """AWS, GitHub and Stripe credentials have no business in a coding-CLI child."""

    for cli_type in ("claude", "codex"):
        env = minimal_environment(build_child_environment(cli_type).as_child_env())
        assert UNRELATED_SECRET not in env.values(), f"{cli_type} inherited an unrelated secret"
        for name in ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "STRIPE_SECRET_KEY"):
            assert name not in env


@pytest.mark.subprocess
def test_real_child_process_sees_only_its_own_credentials(
    all_secrets: None, tmp_path: Path
) -> None:
    """End-to-end through a real subprocess, not just the constructed mapping.

    The child reports every secret-shaped variable it can actually see. This is the assertion that
    would have caught the original bug from either direction: too few variables (the child got
    nothing) or too many (the child got the whole parent environment).
    """

    script = tmp_path / "report_env.py"
    script.write_text(
        "import json, os\n"
        "print(json.dumps(sorted(k for k in os.environ "
        "if any(t in k.upper() for t in ('KEY', 'TOKEN', 'SECRET')))))\n",
        encoding="utf-8",
    )

    env = minimal_environment(build_child_environment("claude").as_child_env())
    result = subprocess.run(
        [sys.executable, str(script)], env=env, capture_output=True, text=True, check=True
    )

    assert json.loads(result.stdout) == ["ANTHROPIC_API_KEY"]
    assert OPENAI_SECRET not in result.stdout
    assert UNRELATED_SECRET not in result.stdout


# --------------------------------------------------------------------------- leak surfaces


def test_plan_repr_does_not_contain_secret_values(all_secrets: None) -> None:
    """A dataclass repr is exactly what ends up in a traceback or a debug log."""

    plan = build_child_environment("claude")

    assert ANTHROPIC_SECRET not in repr(plan)
    # The names are safe and are what makes the diagnostic useful.
    assert plan.secret_env_names == ["ANTHROPIC_API_KEY"]


def test_auth_evidence_carries_names_never_values(
    all_secrets: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Evidence is reported to the user; it must be safe to print verbatim."""

    plan = build_child_environment("claude")
    fake = tmp_path / "claude-missing"

    evidence = probe_claude_auth(str(fake), plan)

    rendered = f"{evidence.detail} {evidence.environment_names} {evidence.conflicts}"
    assert ANTHROPIC_SECRET not in rendered
    assert ANTHROPIC_SECRET not in repr(evidence)
    assert evidence.environment_names == ["ANTHROPIC_API_KEY"]


# --------------------------------------------------------------------------- probe behavior


def test_cli_verdict_overrides_a_stale_credential_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The old file check was wrong in *both* directions; this pins the false-positive half.

    ``~/.claude.json`` is Claude Code's configuration file, not a credential store — it exists on
    machines that have never signed in. The pre-v0.1.5 check treated its mere presence as proof of
    authentication, so OpenAgent reported "authenticated", started the run, and the user got an
    opaque failure from the CLI instead of the actionable "you are not signed in" up front.

    Observed on a real machine during development: ``~/.claude.json`` present, and
    ``claude auth status`` reporting ``loggedIn: false``.
    """

    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    (tmp_path / ".claude.json").write_text('{"theme": "dark"}', encoding="utf-8")

    fake_cli = tmp_path / "claude-stub"
    fake_cli.write_text(
        '#!/bin/sh\necho \'{"loggedIn": false, "authMethod": "none"}\'\n', encoding="utf-8"
    )
    fake_cli.chmod(0o755)

    evidence = probe_claude_auth(str(fake_cli), build_child_environment("claude"))

    assert evidence.authenticated is False, "a config file was mistaken for a credential"
    assert evidence.blocking is True
    assert evidence.source is CliCredentialSource.NONE


def test_cli_reported_key_source_is_named_in_a_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When several credentials are set, say which one the CLI actually chose.

    ``apiKeySource`` comes from the CLI itself, so this is reporting rather than guessing — and it
    is what turns "I rotated my key and nothing changed" into a one-line diagnosis.
    """

    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_SECRET)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-canary")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))

    fake_cli = tmp_path / "claude-stub"
    fake_cli.write_text(
        "#!/bin/sh\n"
        'echo \'{"loggedIn": true, "authMethod": "api_key", '
        '"apiKeySource": "ANTHROPIC_API_KEY"}\'\n',
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)

    evidence = probe_claude_auth(str(fake_cli), build_child_environment("claude"))

    assert evidence.authenticated is True
    assert len(evidence.conflicts) == 1
    assert "ANTHROPIC_API_KEY" in evidence.conflicts[0]
    assert ANTHROPIC_SECRET not in evidence.conflicts[0]


def test_missing_executable_falls_back_to_env_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unrunnable probe must not turn an exported credential into "not authenticated".

    This is the exact failure mode being fixed: OpenAgent could not confirm the credential, so it
    refused to start — even though the credential was present and would have worked.
    """

    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_SECRET)
    plan = build_child_environment("claude")

    evidence = probe_claude_auth(str(tmp_path / "does-not-exist"), plan)

    assert evidence.authenticated is True
    assert evidence.blocking is False
    assert evidence.source is CliCredentialSource.ENV_API_KEY


def test_no_credentials_anywhere_is_a_definite_negative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With nothing set and no login file, the answer is a blocking "no" with an actionable fix."""

    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    plan = build_child_environment("claude")

    evidence = probe_claude_auth(str(tmp_path / "does-not-exist"), plan)

    assert evidence.authenticated is False
    assert evidence.blocking is True
    assert "ANTHROPIC_API_KEY" in evidence.detail  # tells the user what to do


def test_codex_unknown_state_does_not_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ "Could not determine" is not "not authenticated", and must not stop a run."""

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    plan = build_child_environment("codex")

    evidence = probe_codex_auth(str(tmp_path / "does-not-exist"), plan)

    assert evidence.authenticated is None
    assert evidence.blocking is False
    assert evidence.source is CliCredentialSource.UNKNOWN


# --------------------------------------------------------------------------- deprecated alias


def test_codex_api_key_is_forwarded_with_a_deprecation_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CODEX_API_KEY`` was named in OpenAgent's own error message but the CLI never read it.

    Silently ignoring it would reproduce the original failure with a new cause, so it is mapped to
    the variable the CLI actually reads and the user is told to stop using it.
    """

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("CODEX_API_KEY", OPENAI_SECRET)

    plan = build_child_environment("codex")

    assert plan.as_child_env()["OPENAI_API_KEY"] == OPENAI_SECRET
    assert "CODEX_API_KEY" not in plan.as_child_env()
    assert any("deprecated" in notice for notice in plan.notices)


def test_canonical_variable_wins_over_the_deprecated_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_SECRET)
    monkeypatch.setenv("CODEX_API_KEY", "sk-stale-alias-value")

    plan = build_child_environment("codex")

    assert plan.as_child_env()["OPENAI_API_KEY"] == OPENAI_SECRET


# --------------------------------------------------------------------------- conflicts


def test_multiple_credentials_produce_a_warning_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI picks one by its own precedence; saying which is what makes rotation debuggable."""

    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_SECRET)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-canary")

    plan = build_child_environment("claude")
    conflicts = describe_conflicts(plan)

    assert len(conflicts) == 1
    assert "ANTHROPIC_API_KEY" in conflicts[0] and "CLAUDE_CODE_OAUTH_TOKEN" in conflicts[0]
    assert ANTHROPIC_SECRET not in conflicts[0]


def test_single_credential_produces_no_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_SECRET)

    assert describe_conflicts(build_child_environment("claude")) == []


# --------------------------------------------------------------------------- unknown CLIs


def test_unknown_cli_type_yields_an_empty_plan() -> None:
    """An adapter with no documented credential surface gets nothing, not everything."""

    plan = build_child_environment("some-third-party-cli")

    assert plan.as_child_env() == {}
    assert plan.auth_source is CliCredentialSource.NONE
