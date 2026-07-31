"""Which addresses earn the plaintext-HTTP exemption (spec §5.3, §23.3).

Local providers are exempt from TLS because there is no network to intercept on loopback and
forcing HTTPS on ``http://localhost:11434`` would make Ollama unusable out of the box. The
exemption is therefore a claim about the *packets*, not about the provider: a local provider
reached over a network address is a remote connection wearing a local provider's name, and it is
precisely the case where an API key crosses the wire in cleartext.

``0.0.0.0`` is the case that motivated this. It reads as "localhost" to a human and is the address
people paste after seeing a server log ``Listening on 0.0.0.0:11434``, but it is the *unspecified*
address — the wildcard a server binds to in order to accept traffic from every interface. It is not
loopback, and matching it as a literal string handed the exemption to the one address that most
strongly suggests the service is reachable from off-box.
"""

from __future__ import annotations

import pytest

from openagent.providers.spec import is_loopback, requires_tls


def local_http_allowed(url: str) -> bool:
    """Whether a *local* provider may speak plain HTTP to this URL."""

    return not requires_tls(url, local=True)


# --------------------------------------------------------------------------- exempt


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434",
        "http://LOCALHOST:11434",
        "http://127.0.0.1:1234/v1",
        # The whole 127/8 block is loopback, not just .0.1 — a server told to bind 127.0.0.2 is
        # every bit as unreachable from off-box.
        "http://127.0.0.2:11434",
        "http://127.255.255.254:11434",
        "http://[::1]:1234/v1",
    ],
)
def test_loopback_addresses_keep_the_plaintext_exemption(url: str) -> None:
    assert is_loopback(url) is True
    assert local_http_allowed(url) is True


# --------------------------------------------------------------------------- not exempt


@pytest.mark.parametrize(
    "url",
    [
        # The wildcard bind address. Not loopback, however much it looks like it.
        "http://0.0.0.0:11434",
        "http://[::]:11434",
        # LAN, VPN and private-cloud addresses are all off-box.
        "http://192.168.1.50:11434",
        "http://10.8.0.3:11434",
        "http://172.16.4.9:1234/v1",
        "http://ollama.internal:11434",
        "http://203.0.113.7:11434",
    ],
)
def test_non_loopback_addresses_must_use_tls(url: str) -> None:
    assert is_loopback(url) is False
    assert local_http_allowed(url) is False


def test_https_is_always_acceptable() -> None:
    assert requires_tls("https://192.168.1.50:11434", local=True) is False
    assert requires_tls("https://api.example.com", local=False) is False


def test_a_remote_provider_never_gets_the_exemption_even_on_loopback() -> None:
    """The exemption is for providers that are meant to be local, not for anything on 127.0.0.1."""

    assert requires_tls("http://127.0.0.1:8080", local=False) is True


def test_an_unparseable_url_fails_closed() -> None:
    assert requires_tls("http://[oops", local=True) is True
    assert is_loopback("http://[oops") is False
