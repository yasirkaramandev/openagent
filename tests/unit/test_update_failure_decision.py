"""An explicitly requested update failure requires a second user decision."""

from __future__ import annotations

import pytest

from openagent.runtimes.cli.update_policy import (
    PostUpdateFailureChoice,
    PostUpdateFailurePrompt,
    decide_post_update_failure,
)


def _prompt() -> PostUpdateFailurePrompt:
    return PostUpdateFailurePrompt(
        cli_type="codex",
        installed_version="1.0.0",
        expected_version="1.1.0",
        install_source="npm",
        failure_category="verification_failed",
        minimum_supported_version="0.9.0",
        installed_runnable=True,
    )


@pytest.mark.parametrize(
    "choice",
    [
        PostUpdateFailureChoice.CONTINUE_WITH_INSTALLED,
        PostUpdateFailureChoice.CANCEL_RUN,
        PostUpdateFailureChoice.OPEN_DOCTOR,
    ],
)
def test_explicit_second_decision_is_preserved(choice: PostUpdateFailureChoice) -> None:
    decision = decide_post_update_failure(_prompt(), callback=lambda _prompt: choice)
    assert decision is choice


def test_unanswerable_second_prompt_cancels_instead_of_continuing() -> None:
    assert (
        decide_post_update_failure(_prompt(), callback=None) is PostUpdateFailureChoice.CANCEL_RUN
    )


def test_unrunnable_installed_version_cannot_be_overridden_to_continue() -> None:
    prompt = _prompt().__class__(**{**_prompt().__dict__, "installed_runnable": False})
    assert (
        decide_post_update_failure(
            prompt,
            callback=lambda _prompt: PostUpdateFailureChoice.CONTINUE_WITH_INSTALLED,
        )
        is PostUpdateFailureChoice.CANCEL_RUN
    )
