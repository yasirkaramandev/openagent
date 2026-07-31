"""Deterministic unhappy-path coverage for updater metadata boundaries."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openagent.core.models import CliInstallation, CliInstallSource
from openagent.runtimes.cli import updates

pytestmark = pytest.mark.unit


class _Response:
    def __init__(self, chunks: list[bytes], *, host: str = "github.com") -> None:
        self._chunks = chunks
        self.url = SimpleNamespace(host=host)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self):
        yield from self._chunks


class _Client:
    def __init__(self, response: _Response) -> None:
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def stream(self, *_args, **_kwargs):
        return self.response


def _use_response(monkeypatch: pytest.MonkeyPatch, response: _Response) -> None:
    monkeypatch.setattr(updates.httpx, "Client", lambda **_kwargs: _Client(response))


def test_fetch_json_is_bounded_and_requires_an_object(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_response(monkeypatch, _Response([b'{"version":', b'"1.2.3"}']))
    assert updates.fetch_json("https://example.invalid", 1, 100) == {"version": "1.2.3"}

    _use_response(monkeypatch, _Response([b"[]"]))
    with pytest.raises(ValueError, match="not a JSON object"):
        updates.fetch_json("https://example.invalid", 1, 100)

    _use_response(monkeypatch, _Response([b"12345"]))
    with pytest.raises(ValueError, match="exceeds"):
        updates.fetch_json("https://example.invalid", 1, 4)


def test_fetch_bytes_rejects_redirects_empty_and_oversized_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_response(monkeypatch, _Response([b"installer"]))
    assert updates.fetch_bytes("https://example.invalid", 1, 100) == b"installer"

    _use_response(monkeypatch, _Response([b"payload"], host="attacker.invalid"))
    with pytest.raises(ValueError, match="untrusted host"):
        updates.fetch_bytes("https://example.invalid", 1, 100)

    _use_response(monkeypatch, _Response([b"12345"]))
    with pytest.raises(ValueError, match="exceeds"):
        updates.fetch_bytes("https://example.invalid", 1, 4)

    _use_response(monkeypatch, _Response([]))
    with pytest.raises(ValueError, match="empty"):
        updates.fetch_bytes("https://example.invalid", 1, 100)


def test_package_metadata_parsers_fail_closed() -> None:
    def runner(payload: str, returncode: int = 0):
        return lambda *_args: SimpleNamespace(
            returncode=returncode,
            stdout=payload,
            stderr="package manager failed" if returncode else "",
        )

    assert updates._json_command(runner('"1.2.3"'), ["tool"]) == {"value": "1.2.3"}
    assert updates._json_command(runner('{"value":"1.2.3"}'), ["tool"]) == {"value": "1.2.3"}
    with pytest.raises(ValueError, match="non-object"):
        updates._json_command(runner("[]"), ["tool"])
    with pytest.raises(RuntimeError, match="package manager failed"):
        updates._json_command(runner("{}", returncode=2), ["tool"])

    assert updates._latest_npm("codex", runner('"1.2.3"')) == "1.2.3"
    with pytest.raises(ValueError, match="omitted version"):
        updates._latest_npm("codex", runner('""'))

    claude = CliInstallation(
        id="cli_claude",
        type="claude",
        executable="/opt/claude",
        install_source=CliInstallSource.HOMEBREW_CASK,
        release_channel="latest",
    )
    assert updates._latest_brew("claude", claude, runner('{"casks":[{"version":"2.0"}]}')) == "2.0"
    with pytest.raises(ValueError, match="omitted cask"):
        updates._latest_brew("claude", claude, runner('{"casks":[]}'))
    with pytest.raises(ValueError, match="omitted version"):
        updates._latest_brew("claude", claude, runner('{"casks":[{}]}'))


def test_config_reader_handles_typed_store_and_schema_failures(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.json").write_text(
        '{"schema_version":1,"cli_updates":{"check_interval_hours":0}}',
        encoding="utf-8",
    )
    assert updates.load_update_config(tmp_path) == updates.CliUpdateConfig()

    def fail_read(_self, _name, _default):
        raise updates.LockedJsonStoreError("locked")

    monkeypatch.setattr(updates.LockedJsonStore, "get_section", fail_read)
    assert updates.load_update_config(tmp_path) == updates.CliUpdateConfig()
