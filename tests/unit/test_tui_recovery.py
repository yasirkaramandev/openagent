"""Database startup failures open a DB-independent Textual recovery surface."""

from __future__ import annotations

from textual.widgets import Button, Static

from openagent.core.errors import DatabaseMetadataValidationError
from openagent.tui.recovery import DatabaseRecoveryTUI, RecoveryScreen


async def test_recovery_tui_is_db_independent_and_redacted() -> None:
    secret = "prefixless-invalid-metadata-value"
    error = DatabaseMetadataValidationError(
        metadata_key="minimum_reader_version",
        invalid_state="not a parseable version",
        binary_version="0.1.6rc4",
    )
    # The raw corrupt value is deliberately not part of the typed error, and the recovery app takes
    # no OpenAgentApp/Database argument.
    app = DatabaseRecoveryTUI(error)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, RecoveryScreen)
        rendered = "\n".join(str(widget.render()) for widget in app.screen.query(Static))
        assert "Database recovery" in rendered
        assert "minimum_reader_version" in rendered
        assert "0.1.6rc4" in rendered
        assert secret not in rendered
        assert "Traceback" not in rendered
        for selector in (
            "#recovery-version",
            "#recovery-doctor",
            "#recovery-update",
            "#recovery-repair",
            "#recovery-quit",
        ):
            button = app.screen.query_one(selector, Button)
            assert button.display
