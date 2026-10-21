"""``mira plugin`` — inspect, toggle, test and scaffold plugins (mira#208)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

plugin_app = typer.Typer(help="Inspect, enable/disable, test and scaffold plugins")
console = Console()

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@plugin_app.command("list")
def list_plugins() -> None:
    """Show every discovered plugin with its tier and status."""
    from mira_engine.plugins import get_runtime

    runtime = get_runtime()
    table = Table(title="Plugins")
    for col in ("ID", "Version", "Tier", "Status", "Provides", "Reason"):
        table.add_column(col)
    for p in runtime.plugins:
        m = p.manifest
        provides = ""
        if m:
            parts = [
                *(f"channel:{e.name}" for e in m.channels),
                *(f"tool:{e.name}" for e in m.tools),
                *(f"hook:{e.mode}" for e in m.hooks),
                *(f"provider:{e.name}" for e in m.providers),
                *(["skills"] if m.skills else []),
            ]
            provides = ", ".join(parts)
        table.add_row(p.id, m.version if m else "", p.tier or "", p.status, provides, p.reason)
    packs = _skill_packs()
    for pack in packs:
        source = pack.get("source") or {}
        table.add_row(
            pack["id"], pack.get("version", ""), "skill pack", "installed",
            f"skills:{len(pack.get('skills') or [])}", source.get("path", ""),
        )
    console.print(table if runtime.plugins or packs else "No plugins found.")


def _skill_packs() -> list[dict]:
    """Skill packs installed from the desktop app or MIRA Hub (no code)."""
    try:
        from mira_engine.agent.skill_plugins import SkillPluginManager
        from mira_engine.config.paths import get_workspace_path

        plugins = SkillPluginManager(get_workspace_path(None)).list_plugins()
    except Exception:  # noqa: BLE001 - the kernel listing above still stands
        return []
    return [p for p in plugins if (p.get("source") or {}).get("type") not in ("builtin", "kernel")]


def _set_enabled(plugin_id: str, enabled: bool) -> None:
    from mira_engine.plugins.state import PluginState

    PluginState().set_enabled(plugin_id, enabled)
    verb = "enabled" if enabled else "disabled"
    console.print(f"Plugin [cyan]{plugin_id}[/cyan] {verb}. Restart the gateway to apply.")


@plugin_app.command("enable")
def enable(plugin_id: str = typer.Argument(..., help="Plugin id")) -> None:
    """Enable a plugin (also resets a tripped circuit breaker)."""
    _set_enabled(plugin_id, True)


@plugin_app.command("disable")
def disable(plugin_id: str = typer.Argument(..., help="Plugin id")) -> None:
    """Disable a plugin."""
    _set_enabled(plugin_id, False)


@plugin_app.command("test")
def test(
    path: Path = typer.Argument(..., help="Plugin directory (contains plugin.json)"),
    contract_only: bool = typer.Option(False, "--contract-only", help="Skip the plugin's own tests"),
) -> None:
    """Run the kernel's contract checks, then the plugin's own tests."""
    from mira_engine.plugins.testing import check_plugin

    report = check_plugin(path)
    for label in report.passed:
        console.print(f"[green]✓[/green] {label}")
    for problem in report.problems:
        console.print(f"[red]✗[/red] {problem}")
    failed = not report.ok
    tests_dir = path / "tests"
    if not contract_only and tests_dir.is_dir() and report.ok:
        console.print(f"Running {tests_dir} …")
        rc = subprocess.call([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tests_dir)])
        failed = failed or rc != 0
    raise typer.Exit(1 if failed else 0)


_TOOL_TEMPLATE = '''"""{title} — a mira plugin tool."""

from mira_engine.agent.tools.base import Tool


class {cls}(Tool):
    name = "{tool_name}"
    description = "Describe what this tool does and when the agent should use it."
    parameters = {{
        "type": "object",
        "properties": {{"text": {{"type": "string", "description": "Input text."}}}},
        "required": ["text"],
    }}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, text: str, **_):
        return f"{{self.name}}: {{text}}"
'''

_CHANNEL_TEMPLATE = '''"""{title} — a mira plugin channel."""

from mira_engine.bus.events import OutboundMessage
from mira_engine.channels.base import BaseChannel


class {cls}(BaseChannel):
    name = "{channel_name}"
    display_name = "{title}"

    async def start(self) -> None:
        self._running = True
        # Connect to the service and publish inbound messages with
        # self._handle_message(sender_id=..., chat_id=..., content=...).

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        # Deliver msg.content to msg.chat_id.
        return None
'''

_TEST_TEMPLATE = '''from pathlib import Path

from mira_engine.plugins.testing import assert_plugin_contract


def test_plugin_contract():
    assert_plugin_contract(Path(__file__).resolve().parents[1])
'''


@plugin_app.command("new")
def new(
    plugin_id: str = typer.Argument(..., help="New plugin id (lowercase, digits, dashes)"),
    kind: str = typer.Option("tool", "--kind", help="tool or channel"),
    directory: Path = typer.Option(None, "--dir", help="Parent directory (default: ~/.mira/plugins)"),
) -> None:
    """Scaffold a plugin that already passes the contract."""
    from mira_engine.plugins.loader import user_plugins_dir

    if not _ID.match(plugin_id):
        console.print("[red]id must be lowercase letters, digits and dashes[/red]")
        raise typer.Exit(2)
    if kind not in ("tool", "channel"):
        console.print("[red]--kind must be tool or channel[/red]")
        raise typer.Exit(2)
    target = (directory or user_plugins_dir()) / plugin_id
    if target.exists():
        console.print(f"[red]{target} already exists[/red]")
        raise typer.Exit(1)
    snake = plugin_id.replace("-", "_")
    cls = "".join(part.capitalize() for part in plugin_id.split("-"))
    title = plugin_id.replace("-", " ").title()
    (target / "tests").mkdir(parents=True)
    if kind == "tool":
        module, entry_cls = f"{snake}_tool", f"{cls}Tool"
        (target / f"{module}.py").write_text(_TOOL_TEMPLATE.format(title=title, cls=entry_cls, tool_name=snake))
        provides = {"tools": [{"name": snake, "entry": f"{module}:{entry_cls}"}]}
    else:
        module, entry_cls = f"{snake}_channel", f"{cls}Channel"
        (target / f"{module}.py").write_text(_CHANNEL_TEMPLATE.format(title=title, cls=entry_cls, channel_name=plugin_id))
        provides = {"channels": [{"name": plugin_id, "entry": f"{module}:{entry_cls}"}]}
    manifest = {
        "id": plugin_id,
        "version": "0.1.0",
        "plugin_api": ">=1.0,<2.0",
        "description": f"{title} plugin.",
        "provides": provides,
        "permissions": {"network": [], "filesystem": "none", "shell": False, "secrets": []},
        "default_enabled": False,
    }
    (target / "plugin.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (target / "tests" / "test_contract.py").write_text(_TEST_TEMPLATE)
    console.print(f"Created {target}. Next: mira plugin test {target}; mira plugin enable {plugin_id}")
