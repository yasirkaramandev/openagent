import pytest

from openagent.credentials.redaction import (
    REDACTED,
    clear_registered_secrets,
    redact,
    redact_mapping,
    register_secret,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def test_redacts_openai_style_key():
    text = "using key sk-abcdEFGH1234567890zzzz to call the api"
    out = redact(text)
    assert "sk-abcdEFGH1234567890zzzz" not in out
    assert REDACTED in out


def test_redacts_bearer_token():
    out = redact("Authorization header was Bearer abcdef123456789")
    assert "abcdef123456789" not in out


def test_redacts_authorization_header_keeps_label():
    out = redact("Authorization: Bearer supersecrettoken12345")
    assert out.startswith("Authorization:")
    assert "supersecrettoken12345" not in out


def test_redacts_env_style_api_key_keeps_label():
    out = redact("OPENAI_API_KEY=sk-XYZ9876543210abcdef")
    assert out.startswith("OPENAI_API_KEY=")
    assert "sk-XYZ9876543210abcdef" not in out


def test_redacts_github_token():
    out = redact("token gho_0123456789ABCDEFabcdef0123456789")
    assert "gho_0123456789ABCDEFabcdef0123456789" not in out


def test_redact_mapping_is_recursive():
    data = {"note": "key sk-abcdEFGH1234567890zzzz", "nested": {"list": ["Bearer zzzzzzzzzzzz"]}}
    out = redact_mapping(data)
    assert "sk-abcdEFGH1234567890zzzz" not in out["note"]
    assert "zzzzzzzzzzzz" not in out["nested"]["list"][0]


def test_redacts_anthropic_style_key():
    out = redact("key sk-ant-api03-ABCDEF1234567890ghijkl")
    assert "sk-ant-api03-ABCDEF1234567890ghijkl" not in out
    assert REDACTED in out


def test_registered_prefixless_key_is_redacted():
    """Some providers (e.g. several Chinese providers) issue keys with no known prefix."""
    key = "9f8e7d6c5b4a3210zzhaidianprovider"  # no sk-/gh_/Bearer shape
    assert redact(key) == key  # patterns alone can't catch it
    register_secret(key)
    assert redact(f"calling with {key} now") == f"calling with {REDACTED} now"


def test_registered_secret_redacted_inside_diff():
    key = "prefixless-provider-key-abc123xyz"
    register_secret(key)
    diff = f"+API_TOKEN = '{key}'\n-old = 1\n"
    out = redact(diff)
    assert key not in out


def test_short_values_not_registered():
    register_secret("abc")  # too short — must not over-match
    assert redact("abcdef abc abcxyz") == "abcdef abc abcxyz"


def test_empty_string_untouched():
    assert redact("") == ""
    assert redact("hello world") == "hello world"


# --------------------------------------------------------------------------- NVIDIA keys (§19)

NVIDIA_KEY = "nvapi-THIS_IS_A_FAKE_TEST_KEY_123456"


def test_redacts_bare_nvidia_key():
    out = redact(f"using key {NVIDIA_KEY} to call the api")
    assert NVIDIA_KEY not in out
    assert REDACTED in out


def test_redacts_nvidia_key_in_authorization_header():
    out = redact(f"Authorization: Bearer {NVIDIA_KEY}")
    assert NVIDIA_KEY not in out
    assert out.startswith("Authorization:")


def test_redacts_nvidia_key_in_env_assignment():
    out = redact(f"NVIDIA_API_KEY={NVIDIA_KEY}")
    assert NVIDIA_KEY not in out
    assert out.startswith("NVIDIA_API_KEY=")


def test_redacts_nvidia_key_inside_nested_structures():
    payload = {
        "headers": {"Authorization": f"Bearer {NVIDIA_KEY}"},
        "env": [f"NVIDIA_API_KEY={NVIDIA_KEY}"],
        "note": f"the key {NVIDIA_KEY} was used",
    }
    out = redact_mapping(payload)
    assert NVIDIA_KEY not in json_dumps(out)


def test_nvidia_key_redacted_even_without_the_pattern_via_register():
    """A future NVIDIA key format with no recognizable prefix is still scrubbed exactly (§19)."""

    prefixless = "abcd1234efgh5678ijkl9012mnop"
    register_secret(prefixless)
    out = redact(f"Authorization: Bearer {prefixless}")
    assert prefixless not in out


def json_dumps(value) -> str:
    import json

    return json.dumps(value)
