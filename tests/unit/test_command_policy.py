from openagent.core.permissions import DEVELOPMENT, SAFE_EDIT, get_profile
from openagent.security.command_policy import Decision, Purpose, evaluate


def test_git_push_denied():
    assert evaluate("git push origin main").decision is Decision.DENY


def test_npm_publish_denied():
    assert evaluate("npm publish").decision is Decision.DENY


def test_sudo_denied():
    assert evaluate("sudo rm file").decision is Decision.DENY


def test_reading_env_denied():
    assert evaluate("cat .env").decision is Decision.DENY


def test_ssh_key_denied():
    assert evaluate("cat ~/.ssh/id_rsa").decision is Decision.DENY


def test_rm_rf_needs_approval():
    assert evaluate("rm -rf build").decision is Decision.APPROVAL


def test_network_blocked_without_permission():
    assert evaluate("pip install requests", network_allowed=False).decision is Decision.APPROVAL


def test_network_allowed_with_permission():
    """`pip install` runs unattended only for a profile whose job includes installing packages.

    Pre-v0.1.3 this passed with NO profile, because the default was the permissive broad allowlist.
    The default now fails safe (the guarded `inspect` tier), so the profile must say so explicitly.
    """

    assert (
        evaluate(
            "pip install requests", network_allowed=True, profile=get_profile(DEVELOPMENT)
        ).decision
        is Decision.ALLOW
    )
    # …and under safe-edit it still needs a human, network permission or not.
    assert (
        evaluate(
            "pip install requests", network_allowed=True, profile=get_profile(SAFE_EDIT)
        ).decision
        is Decision.APPROVAL
    )


def test_read_only_inspection_is_allowed():
    assert evaluate("ls -la").allowed is True
    assert evaluate("git status").allowed is True


def test_test_runner_is_allowed_only_through_run_tests():
    """`pytest` executes project code: it is a named capability (run_tests), not a generic command.

    Pre-v0.1.3 `evaluate("pytest -q")` was ALLOW for any caller, because `pytest` sat on the same
    flat allowlist as `ls`. Now the generic run_command path must ask, while run_tests may proceed.
    """

    assert evaluate("pytest -q", purpose=Purpose.TEST).decision is Decision.ALLOW
    assert evaluate("pytest -q", purpose=Purpose.COMMAND).decision is Decision.APPROVAL


def test_empty_command_denied():
    assert evaluate("   ").decision is Decision.DENY
