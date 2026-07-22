"""The console entrypoint renders operational failures cleanly, never as a raw traceback (spec §17).

The failure the user hit surfaced as a Pydantic ``ValidationError`` stack trace. Every expected
operational error — a too-old binary, a corrupt record, a blocked migration — must instead reach the
user as a short, redacted, actionable line (or a structured Doctor JSON check), with a stable exit
code. A traceback stays available behind ``OPENAGENT_DEBUG`` for developers.
"""

from __future__ import annotations

import json
import sys

import pytest

from openagent import __main__
from openagent.core.errors import (
    DatabaseReaderCompatibilityError,
    DataValidationError,
    redact_secrets,
)
from openagent.storage.migrations import SchemaTooNewError


def _compat_error() -> DatabaseReaderCompatibilityError:
    return DatabaseReaderCompatibilityError(
        database_schema=13,
        supported_schema_min=1,
        supported_schema_max=12,
        database_writer_version="0.1.6rc3",
        minimum_reader_version="0.1.6rc3",
        binary_version="0.1.3",
        binary_path="/x/bin/openagent",
        repair_commands=["openagent update --repair"],
    )


def _run_main_raising(monkeypatch, exc: Exception, argv: list[str]) -> None:
    def boom() -> None:
        raise exc

    monkeypatch.setattr("openagent.cli.app.app", boom)
    monkeypatch.setattr(sys, "argv", ["openagent", *argv])


def test_compat_error_is_a_clean_line_not_a_traceback(monkeypatch, capsys) -> None:
    _run_main_raising(monkeypatch, _compat_error(), ["provider", "list"])

    with pytest.raises(SystemExit) as exit_info:
        __main__.main()

    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert "database_incompatible" in captured.err
    assert "cannot safely read it" in captured.err
    assert "openagent update --repair" in captured.err
    assert "Traceback" not in captured.err and "Traceback" not in captured.out


def test_data_validation_error_is_reported_cleanly(monkeypatch, capsys) -> None:
    error = DataValidationError(
        table="provider_connections", record_id="provider_z-ai/glm-5.2", error_count=1
    )
    _run_main_raising(monkeypatch, error, ["provider", "list"])

    with pytest.raises(SystemExit) as exit_info:
        __main__.main()

    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert "data_validation" in captured.err
    assert "provider_z-ai/glm-5.2" in captured.err
    assert "Traceback" not in captured.err


def test_doctor_json_emits_a_structured_check(monkeypatch, capsys) -> None:
    _run_main_raising(monkeypatch, _compat_error(), ["doctor", "--json"])

    with pytest.raises(SystemExit) as exit_info:
        __main__.main()

    assert exit_info.value.code == 2
    payload = json.loads(capsys.readouterr().out)
    check = payload["checks"][0]
    assert check["status"] == "fail"
    assert check["data"]["error_type"] == "database_incompatible"
    assert payload["exit_code"] == 2


def test_debug_env_re_raises_for_a_traceback(monkeypatch) -> None:
    monkeypatch.setenv("OPENAGENT_DEBUG", "1")
    _run_main_raising(monkeypatch, _compat_error(), ["provider", "list"])

    with pytest.raises(DatabaseReaderCompatibilityError):
        __main__.main()


def test_operational_error_detail_is_redacted(monkeypatch, capsys) -> None:
    # A SchemaTooNewError is an operational error; if its message ever carried a secret it must be
    # scrubbed before it reaches the terminal.
    _run_main_raising(
        monkeypatch,
        SchemaTooNewError("schema mentions a token sk-ant-CANARYdeadbeef0123 by accident"),
        ["provider", "list"],
    )

    with pytest.raises(SystemExit):
        __main__.main()

    captured = capsys.readouterr()
    assert "sk-ant-CANARYdeadbeef0123" not in captured.err
    assert "[redacted]" in captured.err


def test_redact_secrets_scrubs_common_key_shapes() -> None:
    scrubbed = redact_secrets("key sk-ant-abc123def456 and nvapi-zzz999yyy888 and Bearer tok12345678")
    assert "sk-ant-abc123def456" not in scrubbed
    assert "nvapi-zzz999yyy888" not in scrubbed
    assert "tok12345678" not in scrubbed
    assert scrubbed.count("[redacted]") == 3
