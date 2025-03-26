#! /usr/bin/env python3
from textual.app import App, ComposeResult
from textual.widgets import Footer, Header
from cli.admin import AdminContent


class MarzbanCLI(App):
    """A Textual app to manage marzban"""

    CSS_PATH = "cli/style.tcss"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.theme = "textual-dark"

    BINDINGS = [
        ("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        """Create child widgets for the app."""
        yield Header()
        yield AdminContent(id="admin-content")
        yield Footer()

    def on_mount(self) -> None:
        """Called when the app is mounted."""
        self.action_show_admins()

    def action_show_admins(self) -> None:
        """Show the admins section."""
        self.query_one("#admin-content")

    async def action_quit(self) -> None:
        """An action to quit the app."""
        self.exit()


if __name__ == "__main__":
    app = MarzbanCLI()
    app.run()
