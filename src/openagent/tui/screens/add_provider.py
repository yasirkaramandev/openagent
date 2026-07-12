"""Add-provider screen (spec §31, §30).

Register an API provider without leaving the TUI: connection name, preset, protocol, optional base
URL / region / workspace id, a credential source (OS keychain · env var · no key), and — when a key
is needed — a masked key input. Actions: Test Connection, Save Provider, Cancel.

The key is never displayed after entry and never stored anywhere but the OS keychain. A saved
provider is immediately available in the Add Agent form.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, Select, Static

from ...core.models import Protocol
from ...providers.factory import PRESETS, preset_names

_CRED_SOURCES = [
    ("OS keychain (recommended)", "keychain"),
    ("Environment variable", "env"),
    ("No key (local provider)", "none"),
]
_PROTOCOLS = [
    ("(preset default)", "preset"),
    ("openai-chat", Protocol.OPENAI_CHAT.value),
    ("openai-responses", Protocol.OPENAI_RESPONSES.value),
    ("anthropic-messages", Protocol.ANTHROPIC_MESSAGES.value),
]


class AddProviderScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
        Binding("f10", "save", "Save"),
    ]
    DEFAULT_CSS = """
    AddProviderScreen #form { height: 1fr; padding: 0 2; }
    AddProviderScreen Label { margin: 1 0 0 0; text-style: bold; }
    AddProviderScreen #env-row, AddProviderScreen #key-row { height: auto; }
    AddProviderScreen #action-bar { height: 3; padding: 0 2; background: $panel; }
    AddProviderScreen #action-bar Button { margin: 0 2 0 0; }
    AddProviderScreen #prov-status { padding: 0 2; height: auto; }
    """

    def compose(self) -> ComposeResult:
        presets = [(f"{PRESETS[name].label} ({name})", name) for name in preset_names()]
        yield Header()
        yield Static("Add Provider", classes="screen-title")
        with VerticalScroll(id="form"):
            yield Label("Connection name")
            yield Input(placeholder="e.g. deepseek-main (required)", id="name")
            yield Label("Provider preset")
            yield Select(presets, value="custom", id="preset", allow_blank=False)
            yield Label("Protocol")
            yield Select(_PROTOCOLS, value="preset", id="protocol", allow_blank=False)
            yield Label("Base URL override (optional)")
            yield Input(placeholder="leave blank to use the preset default", id="base_url")
            yield Label("Region (when applicable)")
            yield Input(placeholder="optional", id="region")
            yield Label("Workspace ID (when applicable)")
            yield Input(placeholder="optional", id="workspace_id")
            yield Label("Credential source")
            yield Select(_CRED_SOURCES, value="keychain", id="cred", allow_blank=False)
            with VerticalScroll(id="env-row"):
                yield Label("Environment variable name")
                yield Input(placeholder="e.g. DEEPSEEK_API_KEY", id="key_env")
            with VerticalScroll(id="key-row"):
                yield Label("API key (hidden; stored in the OS keychain)")
                yield Input(placeholder="paste key", password=True, id="api_key")
            yield Static("", id="prov-status")
        with Horizontal(id="action-bar"):
            yield Button("Test Connection", id="test")
            yield Button("Save Provider (Ctrl+S)", variant="success", id="save")
            yield Button("Cancel (Esc)", id="cancel")
        yield Footer()

    def on_mount(self) -> None:
        self._sync_cred_fields()
        self.query_one("#name", Input).focus()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "cred":
            self._sync_cred_fields()

    def _sync_cred_fields(self) -> None:
        cred = self.query_one("#cred", Select).value
        self.query_one("#env-row").display = cred == "env"
        self.query_one("#key-row").display = cred == "keychain"

    # ------------------------------------------------------------------ field collection

    def _params(self) -> dict:
        protocol_val = self.query_one("#protocol", Select).value
        protocol = None if protocol_val == "preset" else Protocol(protocol_val)
        cred = self.query_one("#cred", Select).value
        return {
            "name": self.query_one("#name", Input).value.strip(),
            "provider_type": self.query_one("#preset", Select).value,
            "protocol": protocol,
            "base_url": self.query_one("#base_url", Input).value.strip() or None,
            "region": self.query_one("#region", Input).value.strip() or None,
            "workspace_id": self.query_one("#workspace_id", Input).value.strip() or None,
            "cred": cred,
            "api_key": self.query_one("#api_key", Input).value or None if cred == "keychain" else None,
            "key_env": self.query_one("#key_env", Input).value.strip() or None if cred == "env" else None,
        }

    def _status(self, message: str) -> None:
        self.query_one("#prov-status", Static).update(message)

    # ------------------------------------------------------------------ actions

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "test":
            self.action_test()
        elif event.button.id == "save":
            self.action_save()
        elif event.button.id == "cancel":
            self.app.pop_screen()

    def action_test(self) -> None:
        p = self._params()
        self._status("[dim]testing…[/dim]")
        self.run_worker(self._do_test(p), exclusive=True)

    async def _do_test(self, p: dict) -> None:
        oa = self.app.oa  # type: ignore[attr-defined]
        result = await oa.providers.test_config(
            provider_type=p["provider_type"], protocol=p["protocol"], base_url=p["base_url"],
            region=p["region"], workspace_id=p["workspace_id"],
            api_key=p["api_key"], key_env=p["key_env"],
        )
        if result.ok:
            self._status(f"[green]✓ connection ok[/green] — {result.detail}")
        else:
            self._status(f"[red]✗ {result.detail}[/red]")

    def action_save(self) -> None:
        p = self._params()
        oa = self.app.oa  # type: ignore[attr-defined]
        if not p["name"]:
            self._status("[red]connection name is required[/red]")
            return
        if oa.providers.get(p["name"]):
            self._status(f"[red]provider {p['name']!r} already exists[/red]")
            return
        oa.providers.add(
            name=p["name"], provider_type=p["provider_type"], protocol=p["protocol"],
            base_url=p["base_url"], region=p["region"], workspace_id=p["workspace_id"],
            api_key=p["api_key"], key_env=p["key_env"],
            store_key=p["cred"] == "keychain" and bool(p["api_key"]),
        )
        self.notify(f"provider '{p['name']}' saved")
        self.dismiss(True)
