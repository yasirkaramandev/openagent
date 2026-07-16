"""Secrets must not reach the screen, and the registry that holds them must not leak (spec §8).

Two independent defects.

**1. Escaping is not redaction.** ``safe_markup()`` escaped Rich markup and stripped control
characters, but never redacted. Every artifact path (``artifacts.py``, ``event_log.py``) called
``redact()``, so the *disk* was covered — but the ~129 TUI/CLI call sites went through
``safe_markup()`` only. A provider that echoes the API key back in an error body therefore rendered
that key on screen, in a toast, in a DataTable cell, and in CLI stderr. Escaping made the key *safe
to render*, which is precisely the wrong guarantee.

**2. The registry was a global, unbounded, plaintext set.** ``_REGISTERED`` grew forever, was never
scoped to a run, and was iterated (``sorted(_REGISTERED, ...)``) while other threads could mutate it
— a `RuntimeError: Set changed size during iteration` waiting to happen, in the code path whose job
is to stop secrets escaping.
"""

from __future__ import annotations

import threading

import pytest

from openagent.credentials.redaction import (
    REDACTED,
    active_secret_count,
    clear_registered_secrets,
    redact,
    register_secret,
    secret_scope,
)
from openagent.tui.markup import safe_display, safe_line, safe_markup

# A key with no recognizable prefix: only the exact-value registry can catch it.
PREFIXLESS = "haidian0099887766prefixlesskey"
PREFIXED = "sk-abcdEFGH1234567890wxyz"
NVIDIA = "nvapi-ABCDEFGH12345678wxyz"


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


# --------------------------------------------------------------- §8 display redacts


def test_safe_display_redacts_a_prefixed_key():
    out = safe_display(f"error: invalid api key {PREFIXED} rejected")
    assert PREFIXED not in out
    assert REDACTED in out


def test_safe_display_redacts_a_registered_prefixless_key():
    register_secret(PREFIXLESS)
    out = safe_display(f"provider said: bad key {PREFIXLESS}")
    assert PREFIXLESS not in out


def test_safe_display_redacts_an_nvidia_key():
    out = safe_display(f"nvidia rejected {NVIDIA}")
    assert NVIDIA not in out


def test_safe_display_redacts_an_authorization_header():
    out = safe_display(f"request failed: Authorization: Bearer {PREFIXLESS}")
    assert PREFIXLESS not in out


def test_safe_markup_redacts_because_every_call_site_uses_it():
    """The headline: ~129 TUI/CLI sites call safe_markup, so it must redact, not just escape."""

    register_secret(PREFIXLESS)
    out = safe_markup(f"provider error body echoed {PREFIXLESS} back")
    assert PREFIXLESS not in out, "safe_markup escaped the secret instead of removing it"


def test_safe_line_redacts_too():
    register_secret(PREFIXLESS)
    out = safe_line(f"key {PREFIXLESS}\nsecond line")
    assert PREFIXLESS not in out


# --------------------------------------------------------------- §8 ordering


def test_a_secret_split_by_ansi_codes_cannot_evade_redaction():
    """Control characters must be stripped BEFORE redacting, or they hide the key from the pattern.

    The terminal renders `sk-ABCD\\x1b[0mEFGH...` as one continuous key. If redaction runs first it
    sees two short fragments, matches neither, and stripping then reassembles the secret on screen.
    """

    split = "sk-abcdEFGH\x1b[0m1234567890wxyz"
    out = safe_display(f"key {split} rejected")
    assert "sk-abcdEFGH1234567890wxyz" not in out


def test_a_registered_secret_split_by_control_chars_cannot_evade_redaction():
    register_secret(PREFIXLESS)
    split = "haidian00998877\x1b[0m66prefixlesskey"
    out = safe_display(f"key {split}")
    assert PREFIXLESS not in out


def test_truncation_cannot_reveal_a_partial_secret():
    """Redaction must precede the length limit, or truncation slices a key mid-pattern."""

    out = safe_display(f"aaaa {PREFIXED} bbbb", limit=18)
    assert "sk-abcdEFGH" not in out


def test_truncation_of_a_registered_secret_shows_no_fragment():
    register_secret(PREFIXLESS)
    out = safe_display(f"xx {PREFIXLESS} yy", limit=20)
    assert "haidian00" not in out


def test_display_still_escapes_markup():
    """Redaction must not cost us the escaping that was already there."""

    out = safe_display("[green]forged success[/green]")
    assert "\\[green]" in out


def test_display_still_strips_ansi():
    assert "\x1b" not in safe_display("\x1b[2Jcleared\x1b[H")


def test_single_line_collapses_newlines():
    assert "\n" not in safe_display("a\nb\nc", single_line=True)


def test_display_of_ordinary_text_is_unchanged():
    """The guard must not corrupt normal output."""

    assert safe_display("just a normal message") == "just a normal message"


def test_short_values_are_not_registered_and_do_not_over_match():
    register_secret("abc")
    assert "abc" in safe_display("abc appears in ordinary prose")


# --------------------------------------------------------------- §8 scoped registry


def test_a_scope_releases_its_secret_on_exit():
    with secret_scope(PREFIXLESS):
        assert PREFIXLESS not in redact(f"k {PREFIXLESS}")
    assert active_secret_count() == 0, "the secret outlived its run"
    assert PREFIXLESS in redact(f"k {PREFIXLESS}"), "still registered after the scope closed"


def test_two_concurrent_runs_sharing_a_key_refcount_it():
    """Run A must not un-redact run B's key by finishing first."""

    with secret_scope(PREFIXLESS):
        with secret_scope(PREFIXLESS):
            assert PREFIXLESS not in redact(PREFIXLESS)
        # inner scope closed, but the outer run is still live
        assert PREFIXLESS not in redact(PREFIXLESS), "a shared secret was released too early"
    assert active_secret_count() == 0


def test_a_scope_releases_even_when_the_run_raises():
    with pytest.raises(RuntimeError):
        with secret_scope(PREFIXLESS):
            raise RuntimeError("run failed")
    assert active_secret_count() == 0


def test_distinct_secrets_in_nested_scopes_are_independent():
    with secret_scope(PREFIXLESS):
        with secret_scope(PREFIXED):
            assert active_secret_count() == 2
        assert active_secret_count() == 1
        assert PREFIXLESS not in redact(PREFIXLESS)


def test_scope_ignores_none_and_short_values():
    with secret_scope(None, "abc", PREFIXLESS):
        assert active_secret_count() == 1


def test_concurrent_register_and_redact_do_not_race():
    """`sorted(_REGISTERED)` iterated a set other threads could mutate — this is that RuntimeError."""

    errors: list[BaseException] = []
    stop = threading.Event()

    def churn():
        i = 0
        while not stop.is_set():
            try:
                with secret_scope(f"secret-value-number-{i}"):
                    pass
            except BaseException as exc:  # noqa: BLE001 - recorded and re-raised in the assert
                errors.append(exc)
                return
            i += 1

    def reader():
        while not stop.is_set():
            try:
                redact("some text with sk-abcdEFGH1234567890wxyz inside")
            except BaseException as exc:  # noqa: BLE001 - recorded and re-raised in the assert
                errors.append(exc)
                return

    threads = [threading.Thread(target=churn) for _ in range(4)]
    threads += [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    threading.Event().wait(0.4)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    assert not errors, f"the redaction registry raced: {errors[:3]}"


def test_the_registry_does_not_grow_without_bound():
    for i in range(500):
        with secret_scope(f"transient-secret-value-{i}"):
            pass
    assert active_secret_count() == 0, "finished runs left their secrets registered forever"


def test_repr_of_the_registry_never_contains_a_secret():
    with secret_scope(PREFIXLESS):
        from openagent.credentials import redaction

        assert PREFIXLESS not in repr(redaction._SECRETS)
