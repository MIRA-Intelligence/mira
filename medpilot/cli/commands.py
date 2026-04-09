"""CLI commands for medpilot."""

import asyncio
import json
import os
import select
import signal
import sys
from pathlib import Path

# Force UTF-8 encoding for Windows console
if sys.platform == "win32":
    if sys.stdout.encoding != "utf-8":
        os.environ["PYTHONIOENCODING"] = "utf-8"
        # Re-open stdout/stderr with UTF-8 encoding
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from medpilot import __logo__, __version__
from medpilot.agent.routing import ModelRouter
from medpilot.config.paths import get_workspace_path
from medpilot.config.schema import Config
from medpilot.providers.factory import make_provider
from medpilot.utils.helpers import sync_workspace_templates

app = typer.Typer(
    name="medpilot",
    help=f"{__logo__} medpilot - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}


class SafeFileHistory(FileHistory):
    """FileHistory that sanitizes surrogate characters on write."""

    def store_string(self, string: str) -> None:
        safe = string.encode("utf-8", errors="surrogateescape").decode(
            "utf-8", errors="replace"
        )
        super().store_string(safe)


def _format_model_selection(value: str | list[str] | None) -> str:
    """Render model config values for CLI output."""
    if value is None:
        return "[dim]not set[/dim]"
    if isinstance(value, list):
        return " -> ".join(value) if value else "[dim]not set[/dim]"
    return value

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit


def _flush_pending_tty_input() -> None:
    """Drop unread keypresses typed while the model was generating output."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
    except Exception:
        return

    try:
        import termios
        termios.tcflush(fd, termios.TCIFLUSH)
        return
    except Exception:
        pass

    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            if not os.read(fd, 4096):
                break
    except Exception:
        return


def _restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.)."""
    if _SAVED_TERM_ATTRS is None:
        return
    try:
        import termios
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)
    except Exception:
        pass


def _init_prompt_session() -> None:
    """Create the prompt_toolkit session with persistent file history."""
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # Save terminal state so we can restore it on exit
    try:
        import termios
        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    from medpilot.config.paths import get_cli_history_path

    history_file = get_cli_history_path()
    history_file.parent.mkdir(parents=True, exist_ok=True)

    _PROMPT_SESSION = PromptSession(
        history=SafeFileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,   # Enter submits (single line mode)
    )


def _print_agent_response(
    response: str,
    render_markdown: bool,
    metadata: dict | None = None,
) -> None:
    """Render assistant response with consistent terminal styling."""
    content = response or ""
    body = _response_renderable(content, render_markdown, metadata=metadata)
    console.print()
    console.print(f"[cyan]{__logo__} medpilot[/cyan]")
    console.print(body)
    console.print()


def _response_renderable(
    response: str,
    render_markdown: bool,
    metadata: dict | None = None,
):
    if metadata and metadata.get("render_as") == "text":
        return Text(response or "")
    return Markdown(response or "") if render_markdown else Text(response or "")


def _print_cli_progress_line(content: str, thinking_spinner=None) -> None:
    if thinking_spinner is not None:
        with thinking_spinner.pause():
            console.print(f"  [dim]↳ {content}[/dim]")
        return
    console.print(f"  [dim]↳ {content}[/dim]")


async def _print_interactive_line(content: str) -> None:
    console.print(content)


async def _print_interactive_progress_line(content: str, thinking_spinner=None) -> None:
    if thinking_spinner is not None:
        with thinking_spinner.pause():
            await _print_interactive_line(f"  [dim]↳ {content}[/dim]")
        return
    await _print_interactive_line(f"  [dim]↳ {content}[/dim]")


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display).

    prompt_toolkit natively handles:
    - Multiline paste (bracketed paste mode)
    - History navigation (up/down arrows)
    - Clean display (no ghost characters or artifacts)
    """
    if _PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        with patch_stdout():
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>You:</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc



def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} medpilot v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """medpilot - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


def _load_workspace_template(name: str) -> str:
    from importlib.resources import files as pkg_files

    try:
        path = (pkg_files("medpilot") / "templates" / name)
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""


def _ensure_workspace_bootstrap(workspace: Path) -> list[str]:
    created: list[str] = []
    bootstrap = ("AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "HEARTBEAT.md")
    for name in bootstrap:
        target = workspace / name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_load_workspace_template(name), encoding="utf-8")
            created.append(name)
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    for name in ("MEMORY.md", "HISTORY.md", "history.jsonl"):
        target = memory_dir / name
        if target.exists():
            continue
        text = _load_workspace_template(f"memory/{name}") if name == "MEMORY.md" else ""
        target.write_text(text, encoding="utf-8")
        created.append(f"memory/{name}")
    return created


@app.command()
def onboard(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    wizard: bool = typer.Option(False, "--wizard", help="Run interactive onboarding wizard"),
):
    """Initialize medpilot configuration and workspace."""
    from medpilot.cli.onboard import run_onboard
    from medpilot.config.loader import get_config_path, load_config, save_config, set_config_path
    from medpilot.config.schema import Config

    config_path = Path(config).expanduser().resolve() if config else get_config_path()
    if config:
        set_config_path(config_path)

    if wizard:
        initial = load_config(config_path) if config_path.exists() else Config()
        if workspace:
            initial.agents.defaults.workspace = workspace
        result = run_onboard(initial)
        if not result.should_save:
            console.print("[yellow]No changes were saved.[/yellow]")
            return
        cfg = result.config
        if workspace:
            cfg.agents.defaults.workspace = workspace
        # Merge discovered/default channel fields without overwriting existing user values.
        from medpilot.channels.registry import discover_all

        defaults = cfg.model_dump(by_alias=True)
        channels_data = defaults.get("channels")
        if isinstance(channels_data, dict):
            for name, cls in discover_all().items():
                try:
                    section_defaults = cls.default_config()
                except Exception:
                    continue
                if isinstance(section_defaults, dict):
                    channels_data.setdefault(name, {})
                    if isinstance(channels_data[name], dict):
                        channels_data[name] = _merge_missing_defaults(channels_data[name], section_defaults)
        cfg = Config.model_validate(defaults)
        save_config(cfg, config_path)
        console.print(f"[green]✓[/green] Saved config at {config_path.resolve()}")
    else:
        if config_path.exists():
            console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
            console.print("  [bold]y[/bold] = overwrite with defaults (existing values will be lost)")
            console.print("  [bold]N[/bold] = refresh config, keeping existing values and adding new fields")
            if typer.confirm("Overwrite?"):
                cfg = Config()
                save_config(cfg, config_path)
                console.print(f"[green]✓[/green] Config reset to defaults at {config_path}")
            else:
                cfg = load_config(config_path)
                # Merge discovered/default channel fields without overwriting existing user values.
                from medpilot.channels.registry import discover_all

                defaults = cfg.model_dump(by_alias=True)
                channels_data = defaults.get("channels")
                if isinstance(channels_data, dict):
                    for name, cls in discover_all().items():
                        try:
                            section_defaults = cls.default_config()
                        except Exception:
                            continue
                        if isinstance(section_defaults, dict):
                            channels_data.setdefault(name, {})
                            if isinstance(channels_data[name], dict):
                                channels_data[name] = _merge_missing_defaults(
                                    channels_data[name], section_defaults
                                )
                cfg = Config.model_validate(defaults)
                save_config(cfg, config_path)
                console.print(f"[green]✓[/green] Config refreshed at {config_path} (existing values preserved)")
        else:
            cfg = Config()
            save_config(cfg, config_path)
            console.print(f"[green]✓[/green] Created config at {config_path}")

    cfg = load_config(config_path)
    if workspace:
        cfg.agents.defaults.workspace = workspace
        save_config(cfg, config_path)

    # Create workspace
    workspace_path = get_workspace_path(cfg.workspace_path)

    if not workspace_path.exists():
        workspace_path.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace_path}")

    created = _ensure_workspace_bootstrap(workspace_path)
    sync_workspace_templates(workspace_path)
    for name in created:
        console.print(f"  [dim]Created {name}[/dim]")


    console.print(f"\n{__logo__} medpilot is ready!")
    console.print("\nNext steps:")
    console.print(f"  1. Add your API key to [cyan]{config_path.resolve()}[/cyan]")
    console.print("     Get one at: https://openrouter.ai/keys")
    config_hint = f" --config {config_path.resolve()}" if config else ""
    console.print(f"  2. Chat: [cyan]medpilot agent -m \"Hello!\"{config_hint}[/cyan]")
    console.print(f"  3. Gateway: [cyan]medpilot gateway{config_hint}[/cyan]")
    # console.print("\n[dim]Want Telegram/WhatsApp? See: https://github.com/HKUDS/medpilot#-chat-apps[/dim]")


def _merge_missing_defaults(existing: object, defaults: object) -> object:
    """Recursively fill missing values from defaults without overwriting existing values."""
    if not isinstance(existing, dict) or not isinstance(defaults, dict):
        return existing

    merged = dict(existing)
    for key, value in defaults.items():
        if key not in merged:
            merged[key] = value
        else:
            merged[key] = _merge_missing_defaults(merged[key], value)
    return merged





def _make_provider(config: Config):
    """Create the appropriate LLM provider from config."""
    try:
        return make_provider(config)
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1) from exc


def _make_provider_for_model(config: Config, model: str):
    """Create a provider for a routed model."""
    try:
        return make_provider(config, model)
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1) from exc


def _workspace_cron_store(config: Config) -> Path:
    return config.workspace_path / "cron" / "jobs.json"


def _migrate_cron_store(config: Config) -> None:
    from medpilot.config.paths import get_cron_dir

    workspace_store = _workspace_cron_store(config)
    legacy_store = get_cron_dir() / "jobs.json"
    if workspace_store.exists() or not legacy_store.exists():
        return
    workspace_store.parent.mkdir(parents=True, exist_ok=True)
    legacy_store.replace(workspace_store)


def _as_text_response(response: object) -> str:
    if hasattr(response, "content"):
        return str(getattr(response, "content", "") or "")
    return str(response or "")


def _load_runtime_config(config: str | None = None, workspace: str | None = None) -> Config:
    """Load config and optionally override the active workspace."""
    from medpilot.config.loader import load_config, set_config_path

    config_path = None
    if config:
        config_path = Path(config).expanduser().resolve()
        if not config_path.exists():
            console.print(f"[red]Error: Config file not found: {config_path}[/red]")
            raise typer.Exit(1)
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")

    loaded = load_config(config_path)
    if config_path and config_path.exists():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            legacy_window = ((payload.get("agents") or {}).get("defaults") or {}).get("memoryWindow")
            if legacy_window is not None:
                console.print("[yellow]Notice: agents.defaults.memoryWindow is no longer used.[/yellow]")
        except Exception:
            pass
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return loaded


# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def gateway(
    port: int | None = typer.Option(None, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Start the medpilot gateway."""
    from medpilot.agent.loop import AgentLoop
    from medpilot.bus.queue import MessageBus
    from medpilot.channels.manager import ChannelManager
    from medpilot.cron.service import CronService
    from medpilot.cron.types import CronJob
    from medpilot.heartbeat.service import HeartbeatService
    from medpilot.session.manager import SessionManager

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    config = _load_runtime_config(config, workspace)

    from medpilot.utils.env import auto_activate_env
    auto_activate_env(config.workspace_path)

    gateway_port = port if port is not None else config.gateway.port
    console.print(f"{__logo__} Starting medpilot gateway on port {gateway_port}...")
    sync_workspace_templates(config.workspace_path)
    bus = MessageBus()
    provider = _make_provider(config)
    model_router = ModelRouter(config.agents.defaults)
    default_tz = config.agents.defaults.timezone
    session_manager = SessionManager(config.workspace_path)

    # Create cron service first (callback set after agent creation)
    cron_store_path = _workspace_cron_store(config)
    cron = CronService(cron_store_path)

    # Create agent with cron service
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.primary_model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=int(getattr(config.agents.defaults, "memory_window", 100)),
        reasoning_effort=config.agents.defaults.reasoning_effort,
        brave_api_key=config.tools.web.search.api_key or None,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        timezone=default_tz,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        provider_factory=lambda model: _make_provider_for_model(config, model),
        model_router=model_router,
    )

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        from medpilot.agent.tools.cron import CronTool
        from medpilot.agent.tools.message import MessageTool
        reminder_note = (
            "[Scheduled Task] Timer finished.\n\n"
            f"Task '{job.name}' has been triggered.\n"
            f"Scheduled instruction: {job.payload.message}"
        )

        # Prevent the agent from scheduling new cron jobs during execution
        cron_tool = agent.tools.get("cron")
        cron_token = None
        if isinstance(cron_tool, CronTool):
            cron_token = cron_tool.set_cron_context(True)
        try:
            response = await agent.process_direct(
                reminder_note,
                session_key=f"cron:{job.id}",
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
            )
            response = _as_text_response(response)
        finally:
            if isinstance(cron_tool, CronTool) and cron_token is not None:
                cron_tool.reset_cron_context(cron_token)

        message_tool = agent.tools.get("message")
        if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
            return response

        try:
            from medpilot.utils.evaluator import evaluate_response

            await evaluate_response(
                response=response,
                task_context=reminder_note,
                provider_arg=provider,
                model=agent.model,
            )
        except Exception:
            pass

        if job.payload.deliver and job.payload.to and response:
            from medpilot.bus.events import OutboundMessage
            await bus.publish_outbound(OutboundMessage(
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to,
                content=response
            ))
        return response
    cron.on_job = on_cron_job

    # Create channel manager
    channels = ChannelManager(config, bus)

    def _pick_heartbeat_target() -> tuple[str, str]:
        """Pick a routable channel/chat target for heartbeat-triggered messages."""
        enabled = set(channels.enabled_channels)
        # Prefer the most recently updated non-internal session on an enabled channel.
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if channel in {"cli", "system"}:
                continue
            if channel in enabled and chat_id:
                return channel, chat_id
        # Fallback keeps prior behavior but remains explicit.
        return "cli", "direct"

    # Create heartbeat service
    async def on_heartbeat_execute(tasks: str) -> str:
        """Phase 2: execute heartbeat tasks through the full agent loop."""
        channel, chat_id = _pick_heartbeat_target()

        async def _silent(*_args, **_kwargs):
            pass

        return await agent.process_direct(
            tasks,
            session_key="heartbeat",
            channel=channel,
            chat_id=chat_id,
            on_progress=_silent,
        )

    async def on_heartbeat_notify(response: str) -> None:
        """Deliver a heartbeat response to the user's channel."""
        from medpilot.bus.events import OutboundMessage
        channel, chat_id = _pick_heartbeat_target()
        if channel == "cli":
            return  # No external channel available to deliver to
        await bus.publish_outbound(OutboundMessage(channel=channel, chat_id=chat_id, content=response))

    hb_cfg = config.gateway.heartbeat
    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        provider=provider,
        model=agent.model,
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
    )

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s}s")

    async def run():
        try:
            await cron.start()
            await heartbeat.start()
            await asyncio.gather(
                agent.run(),
                channels.start_all(),
            )
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        finally:
            await agent.close_mcp()
            heartbeat.stop()
            cron.stop()
            agent.stop()
            await channels.stop_all()

    asyncio.run(run())


@app.command()
def serve(
    host: str | None = typer.Option(None, "--host", help="API host"),
    port: int | None = typer.Option(None, "--port", "-p", help="API port"),
    timeout: float | None = typer.Option(None, "--timeout", help="Request timeout (seconds)"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
):
    """Start OpenAI-compatible API server."""
    from aiohttp import web

    from medpilot.agent.loop import AgentLoop
    from medpilot.api.server import create_app
    from medpilot.bus.queue import MessageBus
    from medpilot.session.manager import SessionManager

    cfg = _load_runtime_config(config, workspace)
    sync_workspace_templates(cfg.workspace_path)

    provider = _make_provider(cfg)
    model_router = ModelRouter(cfg.agents.defaults)
    default_tz = cfg.agents.defaults.timezone
    agent_loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=cfg.workspace_path,
        model=cfg.agents.defaults.primary_model,
        temperature=cfg.agents.defaults.temperature,
        max_tokens=cfg.agents.defaults.max_tokens,
        max_iterations=cfg.agents.defaults.max_tool_iterations,
        memory_window=int(getattr(cfg.agents.defaults, "memory_window", 100)),
        reasoning_effort=cfg.agents.defaults.reasoning_effort,
        brave_api_key=cfg.tools.web.search.api_key or None,
        web_proxy=cfg.tools.web.proxy or None,
        exec_config=cfg.tools.exec,
        timezone=default_tz,
        restrict_to_workspace=cfg.tools.restrict_to_workspace,
        session_manager=SessionManager(cfg.workspace_path),
        mcp_servers=cfg.tools.mcp_servers,
        channels_config=cfg.channels,
        provider_factory=lambda model: _make_provider_for_model(cfg, model),
        model_router=model_router,
    )

    api_host = host if host is not None else cfg.api.host
    api_port = port if port is not None else cfg.api.port
    request_timeout = timeout if timeout is not None else cfg.api.timeout
    api_app = create_app(
        agent_loop=agent_loop,
        model_name=cfg.agents.defaults.primary_model,
        request_timeout=request_timeout,
    )
    web.run_app(api_app, host=api_host, port=api_port, print=None)




# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="Session ID"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="Render assistant output as Markdown"),
    logs: bool = typer.Option(False, "--logs/--no-logs", help="Show medpilot runtime logs during chat"),
):
    """Interact with the agent directly."""
    from loguru import logger

    from medpilot.agent.loop import AgentLoop
    from medpilot.bus.queue import MessageBus
    from medpilot.cron.service import CronService

    if workspace is None and sys.stdin.isatty():
        if typer.confirm("Do you want to use the current directory as a project workspace?"):
            workspace = os.getcwd()

    config = _load_runtime_config(config, workspace)
    
    from medpilot.utils.env import auto_activate_env
    auto_activate_env(config.workspace_path)
    
    sync_workspace_templates(config.workspace_path)

    bus = MessageBus()
    provider = _make_provider(config)
    model_router = ModelRouter(config.agents.defaults)
    default_tz = config.agents.defaults.timezone

    # Create cron service for tool usage (no callback needed for CLI unless running)
    cron_store_path = _workspace_cron_store(config)
    cron = CronService(cron_store_path)

    if logs:
        logger.enable("medpilot")
    else:
        logger.disable("medpilot")

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.primary_model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=int(getattr(config.agents.defaults, "memory_window", 100)),
        reasoning_effort=config.agents.defaults.reasoning_effort,
        brave_api_key=config.tools.web.search.api_key or None,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        timezone=default_tz,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        provider_factory=lambda model: _make_provider_for_model(config, model),
        model_router=model_router,
    )

    # Show spinner when logs are off (no output to miss); skip when logs are on
    def _thinking_ctx():
        if logs:
            from contextlib import nullcontext
            return nullcontext()
        # Animated spinner is safe to use with prompt_toolkit input handling
        return console.status("[dim]medpilot is thinking...[/dim]", spinner="dots")

    async def _cli_progress(content: str, *, tool_hint: bool = False) -> None:
        ch = agent_loop.channels_config
        if ch and tool_hint and not ch.send_tool_hints:
            return
        if ch and not tool_hint and not ch.send_progress:
            return
        console.print(f"  [dim]↳ {content}[/dim]")

    if message:
        # Single message mode — direct call, no bus needed
        async def run_once():
            with _thinking_ctx():
                response = await agent_loop.process_direct(message, session_id, on_progress=_cli_progress)
            if hasattr(response, "content"):
                _print_agent_response(
                    getattr(response, "content", ""),
                    render_markdown=markdown,
                    metadata=getattr(response, "metadata", {}) or {},
                )
            else:
                _print_agent_response(str(response), render_markdown=markdown, metadata={})
            await agent_loop.close_mcp()

        asyncio.run(run_once())
    else:
        # Interactive mode — route through bus like other channels
        from medpilot.bus.events import InboundMessage
        _init_prompt_session()
        console.print(f"{__logo__} Interactive mode (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n")

        if ":" in session_id:
            cli_channel, cli_chat_id = session_id.split(":", 1)
        else:
            cli_channel, cli_chat_id = "cli", session_id

        def _handle_signal(signum, frame):
            sig_name = signal.Signals(signum).name
            _restore_terminal()
            console.print(f"\nReceived {sig_name}, goodbye!")
            sys.exit(0)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        # SIGHUP is not available on Windows
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, _handle_signal)
        # Ignore SIGPIPE to prevent silent process termination when writing to closed pipes
        # SIGPIPE is not available on Windows
        if hasattr(signal, 'SIGPIPE'):
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)

        async def run_interactive():
            bus_task = asyncio.create_task(agent_loop.run())
            turn_done = asyncio.Event()
            turn_done.set()
            turn_response: list[str] = []

            async def _consume_outbound():
                while True:
                    try:
                        msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
                        if msg.metadata.get("_progress"):
                            is_tool_hint = msg.metadata.get("_tool_hint", False)
                            ch = agent_loop.channels_config
                            if ch and is_tool_hint and not ch.send_tool_hints:
                                pass
                            elif ch and not is_tool_hint and not ch.send_progress:
                                pass
                            else:
                                console.print(f"  [dim]↳ {msg.content}[/dim]")
                        elif not turn_done.is_set():
                            if msg.content:
                                turn_response.append(msg.content)
                            turn_done.set()
                        elif msg.content:
                            console.print()
                            _print_agent_response(msg.content, render_markdown=markdown)
                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        break

            outbound_task = asyncio.create_task(_consume_outbound())

            try:
                while True:
                    try:
                        _flush_pending_tty_input()
                        user_input = await _read_interactive_input_async()
                        command = user_input.strip()
                        if not command:
                            continue

                        if _is_exit_command(command):
                            _restore_terminal()
                            console.print("\nGoodbye!")
                            break

                        turn_done.clear()
                        turn_response.clear()

                        await bus.publish_inbound(InboundMessage(
                            channel=cli_channel,
                            sender_id="user",
                            chat_id=cli_chat_id,
                            content=user_input,
                        ))

                        with _thinking_ctx():
                            await turn_done.wait()

                        if turn_response:
                            _print_agent_response(turn_response[0], render_markdown=markdown)
                    except KeyboardInterrupt:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
                    except EOFError:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
            finally:
                agent_loop.stop()
                outbound_task.cancel()
                await asyncio.gather(bus_task, outbound_task, return_exceptions=True)
                await agent_loop.close_mcp()

        asyncio.run(run_interactive())


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status(
    config: str | None = typer.Option(None, "--config", help="Path to config.json"),
):
    """Show channel status."""
    from medpilot.channels.registry import discover_all
    from medpilot.config.loader import load_config, set_config_path

    if config:
        set_config_path(Path(config).expanduser().resolve())
    cfg = load_config()

    # Plugin-oriented status output for compatibility tests.
    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")
    table.add_column("Configuration", style="yellow")

    names: set[str] = set(discover_all().keys())
    names.update(getattr(cfg.channels, "model_extra", {}).keys())
    names.update(("telegram", "whatsapp", "discord", "feishu", "mochat", "dingtalk", "email", "slack", "qq", "matrix", "web"))

    for name in sorted(names):
        section = getattr(cfg.channels, name, None)
        if section is None:
            section = (getattr(cfg.channels, "model_extra", None) or {}).get(name, {})
        enabled = bool(getattr(section, "enabled", False) if not isinstance(section, dict) else section.get("enabled", False))
        table.add_row(name, "✓" if enabled else "✗", "")

    console.print(table)


def _get_bridge_dir() -> Path:
    """Get the bridge directory, setting it up if needed."""
    import shutil
    import subprocess

    # User's bridge location
    from medpilot.config.paths import get_bridge_install_dir

    user_bridge = get_bridge_install_dir()

    # Check if already built
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    # Check for npm
    if not shutil.which("npm"):
        console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)

    # Find source bridge: first check package data, then source dir
    pkg_bridge = Path(__file__).parent.parent / "bridge"  # medpilot/bridge (installed)
    src_bridge = Path(__file__).parent.parent.parent / "bridge"  # repo root/bridge (dev)

    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge

    if not source:
        console.print("[red]Bridge source not found.[/red]")
        console.print("Try reinstalling: pip install --force-reinstall medpilot")
        raise typer.Exit(1)

    console.print(f"{__logo__} Setting up bridge...")

    # Copy to user directory
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    # Install and build
    try:
        console.print("  Installing dependencies...")
        subprocess.run(["npm", "install"], cwd=user_bridge, check=True, capture_output=True)

        console.print("  Building...")
        subprocess.run(["npm", "run", "build"], cwd=user_bridge, check=True, capture_output=True)

        console.print("[green]✓[/green] Bridge ready\n")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        if e.stderr:
            console.print(f"[dim]{e.stderr.decode()[:500]}[/dim]")
        raise typer.Exit(1)

    return user_bridge


@channels_app.command("login")
def channels_login(
    channel: str = typer.Argument(..., help="Channel name"),
    force: bool = typer.Option(False, "--force", help="Force re-login"),
    config: str | None = typer.Option(None, "--config", help="Path to config.json"),
):
    """Login for a specific channel (plugin-aware)."""
    import asyncio

    from medpilot.bus.queue import MessageBus
    from medpilot.channels.registry import discover_all
    from medpilot.config.loader import load_config, set_config_path

    if config:
        set_config_path(Path(config).expanduser().resolve())
    cfg = load_config()
    cls = discover_all().get(channel)
    if not cls:
        console.print(f"[red]Unknown channel: {channel}[/red]")
        raise typer.Exit(1)

    section = getattr(cfg.channels, channel, None)
    if section is None:
        section = (getattr(cfg.channels, "model_extra", None) or {}).get(channel, {"enabled": True})
    bus = MessageBus()
    kwargs: dict[str, object] = {}
    if channel in {"telegram", "feishu"}:
        kwargs["groq_api_key"] = getattr(cfg.providers.groq, "api_key", "")
    if channel == "web":
        kwargs["workspace"] = cfg.workspace_path
    inst = cls(section, bus, **kwargs)
    if not hasattr(inst, "login"):
        console.print(f"[red]Channel '{channel}' does not support login[/red]")
        raise typer.Exit(1)
    ok = asyncio.run(inst.login(force=force))
    if ok:
        console.print(f"[green]✓[/green] {channel} login succeeded")
    else:
        raise typer.Exit(1)


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """Show medpilot status."""
    from medpilot.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} medpilot Status\n")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

    if config_path.exists():
        from medpilot.providers.registry import PROVIDERS

        console.print(f"Model: {_format_model_selection(config.agents.defaults.model)}")
        if config.agents.defaults.route_by_complexity:
            console.print("Routing: [green]enabled[/green]")
            console.print(f"  small: {_format_model_selection(config.agents.defaults.small_model)}")
            console.print(f"  medium: {_format_model_selection(config.agents.defaults.medium_model)}")
            console.print(f"  large: {_format_model_selection(config.agents.defaults.large_model)}")
        else:
            console.print("Routing: [dim]disabled[/dim]")

        # Check API keys from registry
        for spec in PROVIDERS:
            p = getattr(config.providers, spec.name, None)
            if p is None:
                continue
            if spec.is_oauth:
                console.print(f"{spec.label}: [green]✓ (OAuth)[/green]")
            elif spec.is_local:
                # Local deployments show api_base instead of api_key
                if p.api_base:
                    console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(p.api_key)
                console.print(f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}")


# ============================================================================
# OAuth Login
# ============================================================================

provider_app = typer.Typer(help="Manage providers")
app.add_typer(provider_app, name="provider")


_LOGIN_HANDLERS: dict[str, callable] = {}


def _register_login(name: str):
    def decorator(fn):
        _LOGIN_HANDLERS[name] = fn
        return fn
    return decorator


@provider_app.command("login")
def provider_login(
    provider: str = typer.Argument(..., help="OAuth provider (e.g. 'openai-codex', 'github-copilot')"),
):
    """Authenticate with an OAuth provider."""
    from medpilot.providers.registry import PROVIDERS

    key = provider.replace("-", "_")
    spec = next((s for s in PROVIDERS if s.name == key and s.is_oauth), None)
    if not spec:
        names = ", ".join(s.name.replace("_", "-") for s in PROVIDERS if s.is_oauth)
        console.print(f"[red]Unknown OAuth provider: {provider}[/red]  Supported: {names}")
        raise typer.Exit(1)

    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Login not implemented for {spec.label}[/red]")
        raise typer.Exit(1)

    console.print(f"{__logo__} OAuth Login - {spec.label}\n")
    handler()


@_register_login("openai_codex")
def _login_openai_codex() -> None:
    try:
        from oauth_cli_kit import get_token, login_oauth_interactive
        token = None
        try:
            token = get_token()
        except Exception:
            pass
        if not (token and token.access):
            console.print("[cyan]Starting interactive OAuth login...[/cyan]\n")
            token = login_oauth_interactive(
                print_fn=lambda s: console.print(s),
                prompt_fn=lambda s: typer.prompt(s),
            )
        if not (token and token.access):
            console.print("[red]✗ Authentication failed[/red]")
            raise typer.Exit(1)
        console.print(f"[green]✓ Authenticated with OpenAI Codex[/green]  [dim]{token.account_id}[/dim]")
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)


@_register_login("github_copilot")
def _login_github_copilot() -> None:
    import asyncio

    console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")

    async def _trigger():
        from litellm import acompletion
        await acompletion(model="github_copilot/gpt-4o", messages=[{"role": "user", "content": "hi"}], max_tokens=1)

    try:
        asyncio.run(_trigger())
        console.print("[green]✓ Authenticated with GitHub Copilot[/green]")
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
