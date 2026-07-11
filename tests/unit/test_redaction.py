from openagent.credentials.redaction import REDACTED, redact, redact_mapping


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


def test_empty_string_untouched():
    assert redact("") == ""
    assert redact("hello world") == "hello world"
