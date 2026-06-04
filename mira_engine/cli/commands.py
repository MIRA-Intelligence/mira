"""CLI commands for mira."""

import asyncio
import json
import os
import select
import signal
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

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
from loguru import logger

from mira_engine import __logo__, __version__
from mira_engine.agent.routing import ModelRouter
from mira_engine.config.paths import get_workspace_path
from mira_engine.config.schema import Config
from mira_engine.providers.factory import make_provider
from mira_engine.providers.oauth_state import ensure_oauth_state_dirs_for_runtime
from mira_engine.utils.helpers import sync_workspace_templates
from mira_engine.utils.migration import run_startup_migrations

run_startup_migrations()

app = typer.Typer(
    name="mira",
    help=f"{__logo__} mira - Personal AI Assistant",
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


def _probe_base_url(url: str | None) -> str:
    """Return a short connectivity status for a provider base URL."""
    if not url:
        return "[dim]n/a[/dim]"
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            return "[yellow]invalid[/yellow]"
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        with socket.create_connection((host, port), timeout=0.8):
            return "[green]reachable[/green]"
    except Exception:
        return "[red]unreachable[/red]"


def _probe_urls_parallel(urls: list[str | None]) -> dict[str, str]:
    """Probe unique URLs in parallel and return url->status map."""
    unique_urls = sorted({u for u in urls if u})
    if not unique_urls:
        return {}
    results: dict[str, str] = {}
    max_workers = min(12, len(unique_urls))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_probe_base_url, url): url for url in unique_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                results[url] = future.result()
            except Exception:
                results[url] = "[red]unreachable[/red]"
    return results


_PROVIDER_DEFAULT_ENDPOINTS: dict[str, str] = {
    # OAuth providers: probe public auth/start domains.
    "github_copilot": "https://github.com",
    "openai_codex": "https://chatgpt.com",
    # SDK/default endpoints for providers that don't expose default_api_base in registry.
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "deepseek": "https://api.deepseek.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "qianfan": "https://qianfan.baidubce.com/v2",
    "groq": "https://api.groq.com/openai/v1",
    # Custom should stay empty until user provides endpoint.
    "azure_openai": "https://YOUR-RESOURCE-NAME.openai.azure.com/openai/deployments/YOUR-DEPLOYMENT",
    "vllm": "http://localhost:8000/v1",
}


_PROVIDER_MODEL_EXAMPLES: dict[str, tuple[str, ...]] = {
    "openai": ("gpt-4o", "gpt-4.1", "o3-mini"),
    "anthropic": ("claude-3-7-sonnet-latest", "claude-opus-4-1"),
    "openrouter": ("openai/gpt-4o-mini", "anthropic/claude-3.7-sonnet"),
    "deepseek": ("deepseek-chat", "deepseek-reasoner"),
    "gemini": ("gemini-2.5-pro", "gemini-2.5-flash"),
    "dashscope": ("qwen-plus", "qwen-max"),
    "moonshot": ("kimi-k2.5", "moonshot-v1-8k"),
    "mistral": ("mistral-large-latest", "ministral-8b-latest"),
    "groq": ("llama-3.3-70b-versatile", "mixtral-8x7b-32768"),
    "ollama": ("llama3.2", "qwen2.5"),
    "vllm": ("meta-llama/Llama-3.1-8B-Instruct", "Qwen/Qwen2.5-7B-Instruct"),
    "ovms": ("meta-llama/Llama-3.1-8B-Instruct",),
    "github_copilot": ("gpt-4o", "gpt-5"),
    "openai_codex": ("gpt-5-codex", "gpt-5.1-codex"),
}


def _provider_probe_url(spec, provider_cfg: object | None) -> str | None:
    """Resolve display/probe URL for a provider."""
    cfg_base = getattr(provider_cfg, "api_base", None) if provider_cfg is not None else None
    if cfg_base:
        return str(cfg_base)
    # Keep custom empty until user explicitly sets endpoint.
    if spec.name == "custom":
        return None
    if spec.default_api_base:
        return spec.default_api_base
    return _PROVIDER_DEFAULT_ENDPOINTS.get(spec.name)


def _validate_model_input(model: str) -> str | None:
    """Validate model input and return normalized model or None."""
    value = model.strip()
    if not value:
        return None
    if any(ch.isspace() for ch in value):
        return None
    return value


def _provider_model_examples(provider_name: str) -> tuple[str, ...]:
    return _PROVIDER_MODEL_EXAMPLES.get(provider_name, ("<provider-model-name>",))


def _prepare_model_default_for_provider(model: str, spec) -> str:
    """Show bare model by default when current value has selected provider prefix."""
    value = (model or "").strip()
    if "/" not in value:
        return value
    prefix, rest = value.split("/", 1)
    prefix_norm = prefix.replace("-", "_").lower()
    if prefix_norm == spec.name:
        return rest
    litellm_norm = (spec.litellm_prefix or "").replace("-", "_").lower()
    if litellm_norm and prefix_norm == litellm_norm:
        return rest
    return value


def _model_matches_provider(model: str, provider_name: str) -> bool:
    """Best-effort check that a model name matches the selected provider."""
    from mira_engine.providers.registry import find_by_name

    spec = find_by_name(provider_name)
    if not spec:
        return True
    model_lower = model.lower()
    if "/" not in model_lower:
        # In onboarding, provider is already selected; bare model names are allowed.
        return True
    prefix = f"{provider_name}/"
    if model_lower.startswith(prefix):
        return True
    if spec.litellm_prefix and model_lower.startswith(f"{spec.litellm_prefix}/"):
        return True
    return any(kw in model_lower for kw in spec.keywords)


def _coerce_model_for_provider(model: str, provider_name: str) -> str:
    """Coerce obviously mismatched models to a safe provider-specific default."""
    value = model.strip()

    # Prepend provider prefix if missing
    if "/" not in value and provider_name != "auto":
        from mira_engine.providers.registry import find_by_name

        spec = find_by_name(provider_name)
        if spec and spec.litellm_prefix:
            value = f"{spec.litellm_prefix}/{value}"

    if _model_matches_provider(value, provider_name):
        return value
    examples = _provider_model_examples(provider_name)
    if not examples:
        return value
    return examples[0]

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

CLI_CTRL_C_EXIT_HINT = "Press Ctrl+C again to quit"
CLI_DOUBLE_CTRL_C_WINDOW_SEC = 2.0

PROMPT_CTRL_C_IGNORE = "ignore"
PROMPT_CTRL_C_SHOW_HINT = "show_hint"
PROMPT_CTRL_C_EXIT = "exit"


def resolve_prompt_ctrl_c_action(
    *,
    turn_done_set: bool,
    sigint_last: float,
    now: float,
    window_sec: float = CLI_DOUBLE_CTRL_C_WINDOW_SEC,
) -> str:
    """Classify Ctrl+C at the ``You:`` prompt (ignore / hint / exit)."""
    if not turn_done_set:
        return PROMPT_CTRL_C_IGNORE
    if sigint_last and now - sigint_last < window_sec:
        return PROMPT_CTRL_C_EXIT
    return PROMPT_CTRL_C_SHOW_HINT


def should_cancel_turn_on_sigint(*, turn_done_set: bool) -> bool:
    """Return True when SIGINT should cancel the in-flight agent turn."""
    return not turn_done_set


async def interrupt_cli_agent_turn(
    *,
    turn_done: asyncio.Event,
    turn_response: list[str],
    turn_skills: set[str],
    dispatch_tasks: list[asyncio.Task],
    cancel_subagents: object | None = None,
    on_interrupted: object | None = None,
    cancel_timeout: float = 1.0,
) -> None:
    """Stop the current CLI turn: unblock the prompt and cancel dispatch tasks."""
    if not turn_done.is_set():
        turn_response.clear()
        turn_skills.clear()
        turn_done.set()
        if on_interrupted is not None:
            on_interrupted()
    for task in dispatch_tasks:
        if not task.done():
            task.cancel()
    if dispatch_tasks:
        try:
            await asyncio.wait_for(
                asyncio.gather(*dispatch_tasks, return_exceptions=True),
                timeout=cancel_timeout,
            )
        except asyncio.TimeoutError:
            pass
    if cancel_subagents is not None:
        try:
            await cancel_subagents()  # type: ignore[misc]
        except Exception:
            pass


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

    from mira_engine.config.paths import get_cli_history_path

    history_file = get_cli_history_path()
    history_file.parent.mkdir(parents=True, exist_ok=True)

    _PROMPT_SESSION = PromptSession(
        history=SafeFileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,   # Enter submits (single line mode)
    )


def _is_llm_error(text: str) -> bool:
    """Return True when the response looks like a provider/LLM error."""
    if not text:
        return False
    t = text.strip()
    return (
        t.startswith("Error:")
        or t.startswith("Error calling LLM:")
        or "Internal Server Error" in t
        or t.startswith("Sorry, I encountered an error calling the AI model.")
    )


def _print_llm_error(
    error_text: str,
    *,
    model: str | None = None,
    provider_name: str | None = None,
) -> None:
    """Print a provider/LLM error with actionable context."""
    raw = error_text.strip()

    # Extract the underlying detail after "Error: "
    detail = raw
    for prefix in ("Error calling LLM: ", "Error: "):
        if raw.startswith(prefix):
            detail = raw[len(prefix):]
            break

    console.print()
    console.print(f"[red]{__logo__} mira — LLM error[/red]")
    console.print()

    if provider_name:
        console.print(f"  [cyan]Provider:[/cyan] {provider_name}")
    if model:
        console.print(f"  [cyan]Model:[/cyan] {model}")

    console.print()
    console.print(f"  [bold red]{detail}[/bold red]")
    console.print()
    console.print("  [dim]The AI model failed to respond. Try again or check your[/dim]")
    console.print("  [dim]API key and network connection.[/dim]")
    console.print()


def _print_agent_response(
    response: str,
    render_markdown: bool,
    metadata: dict | None = None,
) -> None:
    """Render assistant response with consistent terminal styling."""
    content = response or ""
    body = _response_renderable(content, render_markdown, metadata=metadata)
    console.print()
    console.print(f"[cyan]{__logo__} mira[/cyan]")
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
    except EOFError:
        raise



def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} mira v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """mira - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


def _load_workspace_template(name: str) -> str:
    from importlib.resources import files as pkg_files

    try:
        path = (pkg_files("mira_engine") / "templates" / name)
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
    """Initialize mira configuration and workspace."""
    from mira_engine.cli.onboard import run_onboard
    from mira_engine.config.loader import get_config_path, load_config, save_config, set_config_path
    from mira_engine.config.schema import Config
    from mira_engine.providers.registry import PROVIDERS, find_by_name

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
        from mira_engine.channels.registry import discover_all

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
                from mira_engine.channels.registry import discover_all

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

    # Quick provider setup for non-wizard onboarding in interactive terminals.
    if not wizard and sys.stdin.isatty() and sys.stdout.isatty():
        specs = list(PROVIDERS)
        if specs and typer.confirm("Configure a model provider now?", default=True):
            provider_rows: list[tuple[object, object | None, bool, str | None]] = []
            for spec in specs:
                provider_cfg = getattr(cfg.providers, spec.name, None)
                configured = bool(
                    provider_cfg and (
                        provider_cfg.api_base if spec.is_local else (provider_cfg.api_key or spec.is_oauth)
                    )
                )
                shown_url = _provider_probe_url(spec, provider_cfg)
                provider_rows.append((spec, provider_cfg, configured, shown_url))
            probe_map = _probe_urls_parallel([row[3] for row in provider_rows])

            console.print("\nSupported providers:")
            for idx, (spec, _provider_cfg, configured, shown_url) in enumerate(provider_rows, 1):
                mark = " *" if configured else ""
                conn = probe_map.get(shown_url, "[dim]n/a[/dim]") if shown_url else "[dim]n/a[/dim]"
                if shown_url:
                    url_part = f"[dim]{shown_url}[/dim]"
                else:
                    url_part = "[dim](provider uses SDK/default endpoint)[/dim]"
                console.print(
                    f"  {idx}. {spec.label} ({spec.name.replace('_', '-')}){mark}  {url_part}  {conn}"
                )

            while True:
                selected_raw = typer.prompt(
                    "Select provider number (leave empty to skip)", default="", show_default=False
                ).strip()
                if selected_raw == "":
                    break
                if not selected_raw.isdigit():
                    console.print("[yellow]! Please enter a valid number[/yellow]")
                    continue
                selected_idx = int(selected_raw)
                if not 1 <= selected_idx <= len(specs):
                    console.print("[yellow]! Number out of range[/yellow]")
                    continue

                selected = specs[selected_idx - 1]
                cfg.agents.defaults.provider = selected.name
                if selected.is_oauth:
                    save_config(cfg, config_path)
                    _run_oauth_login(selected.name)
                else:
                    selected_cfg = getattr(cfg.providers, selected.name, None)
                    if selected_cfg is not None:
                        # Custom provider requires explicit api_base configuration
                        if selected.name == "custom":
                            has_existing_base = bool(selected_cfg.api_base)
                            if has_existing_base:
                                base_action = typer.prompt(
                                    "API Base URL",
                                    type=typer.Choice(["update", "keep", "clear"]),
                                    default="keep",
                                )
                                if base_action == "update":
                                    api_base = typer.prompt(
                                        "API Base URL (e.g., http://localhost:8000/v1)",
                                        default=selected_cfg.api_base,
                                    ).strip()
                                    selected_cfg.api_base = api_base
                                elif base_action == "clear":
                                    selected_cfg.api_base = ""
                            else:
                                api_base = typer.prompt(
                                    "API Base URL (required, e.g., http://localhost:8000/v1)",
                                    default="",
                                ).strip()
                                selected_cfg.api_base = api_base
                        else:
                            # Other providers: use default api_base if available
                            if selected.default_api_base and not selected_cfg.api_base:
                                selected_cfg.api_base = selected.default_api_base

                        api_key = typer.prompt(
                            f"API key for {selected.label} (optional, hidden input)",
                            default=selected_cfg.api_key or "",
                            show_default=False,
                            hide_input=True,
                        ).strip()
                        if api_key:
                            selected_cfg.api_key = api_key
                        setattr(cfg.providers, selected.name, selected_cfg)

                examples = ", ".join(_provider_model_examples(selected.name))
                console.print(
                    f"[dim]Model examples for {selected.label}:[/dim] {examples}\n"
                    "[dim]Tip:[/dim] after provider is selected, you can input model name without provider prefix."
                )

                current_model = cfg.agents.defaults.model or ""
                model_default = _prepare_model_default_for_provider(current_model, selected)

                while True:
                    model_input = typer.prompt(
                        "Model name (required)",
                        default=model_default,
                        show_default=bool(model_default),
                    )
                    normalized_model = _validate_model_input(model_input)
                    if not normalized_model:
                        console.print("[yellow]! Invalid model name (empty or contains spaces)[/yellow]")
                        continue
                    if not _model_matches_provider(normalized_model, selected.name):
                        if not typer.confirm(
                            "Model name may not match selected provider. Continue anyway?",
                            default=False,
                        ):
                            continue
                    cfg.agents.defaults.model = _coerce_model_for_provider(normalized_model, selected.name)
                    if cfg.agents.defaults.model != normalized_model:
                        console.print(
                            f"[yellow]! Model '{normalized_model}' does not match provider '{selected.name}', "
                            f"using '{cfg.agents.defaults.model}' instead.[/yellow]"
                        )
                    break

                save_config(cfg, config_path)

                docs_url = (
                    "Run `mira onboard` and choose this provider to start OAuth login."
                    if selected.is_oauth
                    else f"https://docs.litellm.ai/docs/providers/{selected.name.replace('_', '-')}"
                )
                console.print(
                    f"[dim]Using provider:[/dim] {selected.name}\n"
                    f"[dim]How to use:[/dim] set `agents.defaults.model` to a model from this provider, "
                    f"then run `mira status`.\n"
                    f"[dim]Provider docs:[/dim] {docs_url}"
                )
                break

    # Create workspace
    workspace_path = get_workspace_path(cfg.workspace_path)

    if not workspace_path.exists():
        workspace_path.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace_path}")

    created = _ensure_workspace_bootstrap(workspace_path)
    sync_workspace_templates(workspace_path)
    for name in created:
        console.print(f"  [dim]Created {name}[/dim]")


    console.print(f"\n{__logo__} mira is ready!")
    console.print("\nNext steps:")
    console.print(f"  1. Add your API key to [cyan]{config_path.resolve()}[/cyan]")
    provider_docs = "https://openrouter.ai/keys"
    if cfg.agents.defaults.provider != "auto":
        spec = find_by_name(cfg.agents.defaults.provider)
        if spec:
            provider_docs = f"https://docs.litellm.ai/docs/providers/{spec.name.replace('_', '-')}"
    console.print(f"     Provider docs: {provider_docs}")
    config_hint = f" --config {config_path.resolve()}" if config else ""
    console.print(f"  2. Chat: [cyan]mira agent -m \"Hello!\"{config_hint}[/cyan]")
    console.print(f"  3. Gateway: [cyan]mira gateway{config_hint}[/cyan]")


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
    from mira_engine.config.paths import get_cron_dir

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
    from mira_engine.config.loader import load_config, set_config_path

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


def _sync_workspace_templates_or_exit(workspace: Path) -> None:
    """Initialize workspace templates or fail with an actionable config error."""
    try:
        sync_workspace_templates(workspace)
    except OSError as exc:
        from mira_engine.config.loader import get_config_path

        console.print("[red]Error: Mira workspace is not accessible.[/red]")
        console.print(f"Workspace: {workspace}")
        console.print(f"Config: {get_config_path()}")
        console.print(
            "Update agents.defaults.workspace in the active config, or choose a valid Workspace path in MIRA Settings."
        )
        console.print(f"Original error: {exc}")
        raise typer.Exit(1) from exc


# ============================================================================
# Gateway / Server
# ============================================================================


def _gateway_failsafe_check(gateway_host: str, gateway_port: int, verbose: bool = False) -> None:
    """Check for existing Mira instances by PID file and port."""
    import atexit
    import os
    import socket
    from pathlib import Path

    import psutil

    if os.environ.get("MIRA_SKIP_GATEWAY_FAILSAVE"):
        return

    pid_file = Path("~/.mira/runtime/gateway.pid").expanduser()
    pid_file.parent.mkdir(parents=True, exist_ok=True)

    # 1. 检查 PID 文件
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            if psutil.pid_exists(old_pid):
                proc = psutil.Process(old_pid)
                if "mira" in " ".join(proc.cmdline()):
                    # Note: Using basic print here as it is before main gateway loop setup
                    print(f"错误: Mira 已经在运行中 (PID: {old_pid})。")
                    print("提示: 请先停止旧进程，或使用 `mira-engine stop`。")
                    raise typer.Exit(1)
        except (ValueError, psutil.NoSuchProcess, psutil.AccessDenied, typer.Exit):
            if isinstance(sys.exc_info()[1], typer.Exit):
                raise
            pass

    # 2. 检查端口占用
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            check_host = "127.0.0.1" if gateway_host == "0.0.0.0" else gateway_host
            if s.connect_ex((check_host, gateway_port)) == 0:
                print(f"错误: 端口 {gateway_port} 已被占用。")
                print("提示: 这通常意味着 Mira 已经在运行中。请检查系统进程。")
                raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as e:
        if verbose:
            print(f"端口探测异常: {e}")

    # 3. 写入当前 PID
    pid_file.write_text(str(os.getpid()))
    atexit.register(lambda: pid_file.unlink(missing_ok=True))


@app.command()
def gateway(
    host: str | None = typer.Option(None, "--host", help="Gateway host"),
    port: int | None = typer.Option(None, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Start the mira gateway."""
    from mira_engine.agent.loop import AgentLoop
    from mira_engine.bus.queue import MessageBus
    from mira_engine.channels.manager import ChannelManager
    from mira_engine.cron.service import CronService
    from mira_engine.cron.types import CronJob
    from mira_engine.heartbeat.service import HeartbeatService
    from mira_engine.session.manager import SessionManager

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    config = _load_runtime_config(config, workspace)

    if host is not None:
        config.gateway.host = host

    if port is not None:
        config.gateway.port = port

    gateway_host = config.gateway.host
    gateway_port = config.gateway.port

    _gateway_failsafe_check(gateway_host, gateway_port, verbose)

    console.print(f"{__logo__} Starting mira gateway on {gateway_host}:{gateway_port}...")
    _sync_workspace_templates_or_exit(config.workspace_path)
    bus = MessageBus()
    provider = _make_provider(config)
    model_router = ModelRouter(config.agents.defaults)
    provider_factory = lambda model: _make_provider_for_model(config, model)
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
        provider_factory=provider_factory,
        model_router=model_router,
    )

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        from mira_engine.agent.tools.cron import CronTool
        from mira_engine.agent.tools.message import MessageTool
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
            from mira_engine.utils.evaluator import evaluate_response

            await evaluate_response(
                response=response,
                task_context=reminder_note,
                provider_arg=provider,
                model=agent.model,
            )
        except Exception:
            pass

        if job.payload.deliver and job.payload.to and response:
            from mira_engine.bus.events import OutboundMessage
            await bus.publish_outbound(OutboundMessage(
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to,
                content=response
            ))
        return response
    cron.on_job = on_cron_job

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
        from mira_engine.bus.events import OutboundMessage
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

    async def on_ui_runtime_config_updated(next_config: Config, projects_root: Path) -> None:
        nonlocal config, provider, model_router, provider_factory, default_tz, session_manager

        next_provider = _make_provider(next_config)
        next_model_router = ModelRouter(next_config.agents.defaults)
        next_provider_factory = lambda model: _make_provider_for_model(next_config, model)
        next_tz = next_config.agents.defaults.timezone
        next_workspace = projects_root.expanduser()

        await agent.reconfigure_runtime(
            provider=next_provider,
            model=next_config.agents.defaults.primary_model,
            provider_factory=next_provider_factory,
            model_router=next_model_router,
            workspace=next_workspace,
            max_iterations=next_config.agents.defaults.max_tool_iterations,
            max_tokens=next_config.agents.defaults.max_tokens,
            reasoning_effort=next_config.agents.defaults.reasoning_effort,
            restrict_to_workspace=next_config.tools.restrict_to_workspace,
            brave_api_key=next_config.tools.web.search.api_key or None,
            web_proxy=next_config.tools.web.proxy or None,
            exec_config=next_config.tools.exec,
            timezone=next_tz,
            channels_config=next_config.channels,
            context_window_tokens=next_config.agents.defaults.context_window_tokens,
        )

        heartbeat.provider = next_provider
        heartbeat.model = agent.model
        heartbeat.workspace = next_workspace
        heartbeat.interval_s = next_config.gateway.heartbeat.interval_s
        heartbeat.enabled = next_config.gateway.heartbeat.enabled
        session_manager = agent.sessions

        config = next_config
        provider = next_provider
        model_router = next_model_router
        provider_factory = next_provider_factory
        default_tz = next_tz
        logger.info("Gateway runtime config reloaded from UI settings")

    # Create channel manager after the reload callback exists so UI config
    # saves can update the live agent runtime without restarting the service.
    channels = ChannelManager(
        config,
        bus,
        on_ui_runtime_config_updated=on_ui_runtime_config_updated,
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

    from mira_engine.agent.loop import AgentLoop
    from mira_engine.api.server import create_app
    from mira_engine.bus.queue import MessageBus
    from mira_engine.session.manager import SessionManager

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


def _configure_cli_logging(logs_mode: bool) -> None:
    """Toggle loguru sinks for the interactive CLI.

    ``logger.disable(name)`` only matches loggers whose ``__name__`` equals
    ``name`` or starts with ``name + "."``. Mira engine modules emit under
    the ``mira_engine`` namespace (no dot suffix from ``mira``), so we must
    disable both prefixes explicitly to keep the prompt clean. Same on the
    enable path so ``--logs`` actually surfaces every engine line.
    """
    from loguru import logger

    namespaces = ("mira", "mira_engine")
    if logs_mode:
        for ns in namespaces:
            logger.enable(ns)
    else:
        for ns in namespaces:
            logger.disable(ns)


def _build_agent_loop_kwargs(
    *,
    bus,
    provider,
    config: Config,
    cron_service=None,
    model_router=None,
) -> dict[str, object]:
    """Common keyword arguments shared by ``mira agent`` and ``mira research``."""
    default_tz = config.agents.defaults.timezone
    return dict(
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
        cron_service=cron_service,
        timezone=default_tz,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        provider_factory=lambda model: _make_provider_for_model(config, model),
        model_router=model_router,
    )


def _run_cli_agent_session(
    *,
    agent_loop,
    bus,
    message: str | None,
    session_id: str,
    markdown: bool,
    verbose_mode: bool,
    logs_mode: bool,
    inbound_metadata: dict[str, object] | None = None,
    interactive_banner: str | None = None,
    model_name: str | None = None,
    provider_name: str | None = None,
) -> None:
    """Drive a single message or REPL session against ``agent_loop``.

    Shared between ``mira agent`` (general) and ``mira research`` (research
    superset). ``inbound_metadata`` is merged into every InboundMessage so
    callers can pre-populate fields like ``run_mode`` / ``agent_profile`` /
    ``automation_policy``.
    """
    inbound_metadata = dict(inbound_metadata or {})

    def _thinking_ctx():
        if logs_mode:
            from contextlib import nullcontext
            return nullcontext()
        return console.status("[dim]mira is thinking...[/dim]", spinner="dots")

    async def _cli_progress(content: str, *, tool_hint: bool = False) -> None:
        ch = agent_loop.channels_config
        if ch and tool_hint and not ch.send_tool_hints:
            return
        if ch and not tool_hint and not ch.send_progress:
            return
        console.print(f"  [dim]↳ {content}[/dim]")

    if message:
        async def run_once():
            invoked_skills: set[str] = set()

            async def _cli_audit_once(details: dict[str, object]) -> None:
                event = str(details.get("tool", "") or "")
                if event != "read_file":
                    return
                skill_name = str(details.get("skill_name", "") or "").strip()
                if not skill_name:
                    return
                if skill_name not in invoked_skills:
                    invoked_skills.add(skill_name)
                    console.print(f"  [cyan]↳ skill:[/cyan] {skill_name}")

            with _thinking_ctx():
                if verbose_mode:
                    response = await agent_loop.process_direct(
                        message,
                        session_id,
                        on_progress=_cli_progress,
                        audit_hook=_cli_audit_once,
                        metadata=inbound_metadata,
                    )
                else:
                    response = await agent_loop.process_direct(
                        message,
                        session_id,
                        on_progress=_cli_progress,
                        metadata=inbound_metadata,
                    )
            if hasattr(response, "content"):
                resp_text = getattr(response, "content", "")
                resp_meta = getattr(response, "metadata", {}) or {}
            else:
                resp_text = str(response)
                resp_meta = {}

            if _is_llm_error(resp_text):
                _print_llm_error(
                    resp_text,
                    model=model_name or getattr(agent_loop, "model", None),
                    provider_name=provider_name,
                )
            else:
                _print_agent_response(
                    resp_text,
                    render_markdown=markdown,
                    metadata=resp_meta,
                )
            if verbose_mode:
                used = ", ".join(sorted(invoked_skills)) if invoked_skills else "none"
                console.print(f"  [cyan]↳ skills used:[/cyan] {used}")
            await agent_loop.close_mcp()

        asyncio.run(run_once())
        return

    # Interactive mode — route through bus like other channels
    from mira_engine.bus.events import InboundMessage
    _init_prompt_session()
    banner = interactive_banner or (
        f"{__logo__} Interactive mode (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to interrupt, "
        "[bold]Ctrl+C x2[/bold] at prompt to quit)\n"
    )
    console.print(banner)

    if ":" in session_id:
        cli_channel, cli_chat_id = session_id.split(":", 1)
    else:
        cli_channel, cli_chat_id = "cli", session_id

    # Double-Ctrl+C to exit.
    # First Ctrl+C: cancels the current agent turn (interrupts the request).
    # Second Ctrl+C within 2 s: exits the session entirely.
    _sigint_last = [0.0]
    _cli_session_key = f"{cli_channel}:{cli_chat_id}"
    _turn_done_ref: list[asyncio.Event | None] = [None]
    _loop_ref: list[asyncio.AbstractEventLoop | None] = [None]
    _cancel_turn_ref: list = [None]
    _shutdown_ref: list = [None]

    class _CliSessionExit(Exception):
        """Raised to end the interactive loop without sys.exit() in a signal handler."""

    def _schedule_on_loop(coro_factory) -> None:
        loop = _loop_ref[0]
        if loop is None:
            return
        loop.call_soon_threadsafe(lambda: loop.create_task(coro_factory()))

    def _handle_sigint(signum, frame):
        now = time.monotonic()
        evt = _turn_done_ref[0]
        at_prompt = evt is None or evt.is_set()
        cancel_turn = _cancel_turn_ref[0]
        if should_cancel_turn_on_sigint(turn_done_set=not at_prompt) and cancel_turn is not None:
            _sigint_last[0] = now
            # Agent turn in progress — cancel on the event loop; never sys.exit here.
            _schedule_on_loop(cancel_turn)
            return
        # At the prompt — first/second press handled in the KeyboardInterrupt handler below.
        raise KeyboardInterrupt

    def _handle_term(signum, frame):
        shutdown = _shutdown_ref[0]
        if shutdown is not None:
            _schedule_on_loop(shutdown)
            return
        _restore_terminal()
        sig_name = signal.Signals(signum).name
        console.print(f"\nReceived {sig_name}, goodbye!")
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_term)
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, _handle_term)
    if hasattr(signal, 'SIGPIPE'):
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    if hasattr(signal, 'SIGTTOU'):
        signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    if hasattr(signal, 'SIGTTIN'):
        signal.signal(signal.SIGTTIN, signal.SIG_IGN)

    async def run_interactive():
        _loop_ref[0] = asyncio.get_running_loop()
        bus_task = asyncio.create_task(agent_loop.run())
        turn_done = asyncio.Event()
        _turn_done_ref[0] = turn_done
        turn_done.set()
        turn_response: list[str] = []
        turn_skills: set[str] = set()

        async def _collect_dispatch_tasks() -> list[asyncio.Task]:
            tasks = list(agent_loop._active_tasks.get(_cli_session_key, []))
            if not tasks:
                tasks = [
                    t
                    for task_list in agent_loop._active_tasks.values()
                    for t in task_list
                    if not t.done()
                ]
            return tasks

        _cancel_turn_in_flight = [False]

        async def _cancel_current_turn() -> None:
            """Cancel in-flight agent work for this CLI session (like /stop)."""
            if _cancel_turn_in_flight[0]:
                return
            _cancel_turn_in_flight[0] = True
            try:
                await _cancel_current_turn_body()
            finally:
                _cancel_turn_in_flight[0] = False

        async def _cancel_current_turn_body() -> None:
            tasks = await _collect_dispatch_tasks()

            async def _cancel_subagents() -> None:
                await agent_loop.subagents.cancel_by_session(_cli_session_key)

            await interrupt_cli_agent_turn(
                turn_done=turn_done,
                turn_response=turn_response,
                turn_skills=turn_skills,
                dispatch_tasks=tasks,
                cancel_subagents=_cancel_subagents,
                on_interrupted=lambda: console.print("\n\n[dim]Interrupted[/dim]"),
            )
            _sigint_last[0] = 0.0

        _cancel_turn_ref[0] = _cancel_current_turn

        async def _shutdown_cli_session() -> None:
            await _cancel_current_turn()
            _restore_terminal()
            console.print("\nGoodbye!")
            raise _CliSessionExit()

        _shutdown_ref[0] = _shutdown_cli_session

        async def _consume_outbound():
            while True:
                try:
                    msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
                    if msg.metadata.get("_audit_only"):
                        if verbose_mode and msg.metadata.get("_audit_event") == "skill_invoked":
                            details = msg.metadata.get("_audit_details") or {}
                            if isinstance(details, dict):
                                skill_name = str(details.get("skill_name", "") or "").strip()
                                if skill_name:
                                    turn_skills.add(skill_name)
                                    console.print(f"  [cyan]↳ skill:[/cyan] {skill_name}")
                        continue
                    if msg.metadata.get("_progress"):
                        if turn_done.is_set():
                            continue
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
                _flush_pending_tty_input()
                try:
                    user_input = await _read_interactive_input_async()
                except KeyboardInterrupt:
                    evt = _turn_done_ref[0]
                    action = resolve_prompt_ctrl_c_action(
                        turn_done_set=evt.is_set() if evt is not None else True,
                        sigint_last=_sigint_last[0],
                        now=time.monotonic(),
                    )
                    if action == PROMPT_CTRL_C_IGNORE:
                        continue
                    if action == PROMPT_CTRL_C_EXIT:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        return
                    _sigint_last[0] = time.monotonic()
                    console.print(f"\n[dim]{CLI_CTRL_C_EXIT_HINT}[/dim]")
                    continue

                command = user_input.strip()
                if not command:
                    continue

                if _is_exit_command(command):
                    _restore_terminal()
                    console.print("\nGoodbye!")
                    return

                turn_done.clear()
                turn_response.clear()
                turn_skills.clear()
                _sigint_last[0] = 0.0

                turn_metadata = dict(inbound_metadata)
                if verbose_mode:
                    turn_metadata["_emit_skill_audit"] = True

                await bus.publish_inbound(InboundMessage(
                    channel=cli_channel,
                    sender_id="user",
                    chat_id=cli_chat_id,
                    content=user_input,
                    metadata=turn_metadata,
                ))

                with _thinking_ctx():
                    await turn_done.wait()

                if turn_response:
                    _print_agent_response(turn_response[0], render_markdown=markdown)
                if verbose_mode:
                    used = ", ".join(sorted(turn_skills)) if turn_skills else "none"
                    console.print(f"  [cyan]↳ skills used:[/cyan] {used}")
        except _CliSessionExit:
            pass
        except EOFError:
            _restore_terminal()
            console.print("\nGoodbye!")
        finally:
            outbound_task.cancel()
            bus_task.cancel()
            await asyncio.gather(bus_task, outbound_task, return_exceptions=True)

    asyncio.run(run_interactive())


def _build_research_inbound_metadata(
    *,
    mode: str,
    profile: str,
    max_tokens: int | None,
    max_experiments: int | None,
    project_dir: str | None,
) -> dict[str, object]:
    """Translate ``mira research`` flags into InboundMessage.metadata fields."""
    metadata: dict[str, object] = {
        "run_mode": mode,
        "agent_profile": profile,
    }
    automation_policy: dict[str, object] = {}
    if max_tokens is not None:
        automation_policy["maxTokens"] = max_tokens
    if max_experiments is not None:
        automation_policy["maxExperiments"] = max_experiments
    if automation_policy:
        # Preserve the goals/logic shape expected by ResearchAgentLoop's
        # parser even when only thresholds are specified.
        automation_policy.setdefault("logic", "AND")
        automation_policy.setdefault("goals", [])
        metadata["automation_policy"] = automation_policy
    if project_dir:
        metadata["project_dir"] = project_dir
    return metadata


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="Session ID"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="Render assistant output as Markdown"),
    logs: bool = typer.Option(False, "--logs/--no-logs", help="Show mira runtime logs during chat"),
    verbose: bool = typer.Option(False, "--verbose/--no-verbose", help="Show verbose runtime hints (including invoked skills)"),
    debug: bool = typer.Option(False, "--debug/--no-debug", help="Alias of --verbose"),
):
    """Interact with the general-purpose agent (no research orchestration)."""
    from mira_engine.agent.base_loop import BaseAgentLoop
    from mira_engine.bus.queue import MessageBus
    from mira_engine.cron.service import CronService

    if workspace is None and sys.stdin.isatty():
        if typer.confirm("Do you want to use the current directory as a project workspace?"):
            workspace = os.getcwd()

    config = _load_runtime_config(config, workspace)

    sync_workspace_templates(config.workspace_path)

    bus = MessageBus()
    provider = _make_provider(config)
    model_router = ModelRouter(config.agents.defaults)

    cron_store_path = _workspace_cron_store(config)
    cron = CronService(cron_store_path)

    verbose_mode = verbose or debug
    # In interactive chat, verbose output is more stable than raw runtime logs.
    # Keep --debug useful (skill/tool visibility) without TTY log interleaving.
    logs_mode = logs or (debug and message is not None)

    _configure_cli_logging(logs_mode)

    agent_loop = BaseAgentLoop(
        **_build_agent_loop_kwargs(
            bus=bus,
            provider=provider,
            config=config,
            cron_service=cron,
            model_router=model_router,
        ),
    )

    _run_cli_agent_session(
        agent_loop=agent_loop,
        bus=bus,
        message=message,
        session_id=session_id,
        markdown=markdown,
        verbose_mode=verbose_mode,
        logs_mode=logs_mode,
        inbound_metadata=None,
        model_name=config.agents.defaults.primary_model,
        provider_name=config.agents.defaults.provider,
    )


@app.command()
def research(
    message: str = typer.Option(None, "--message", help="Message to send to the research agent"),
    session_id: str = typer.Option("cli:research", "--session", "-s", help="Session ID"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    mode: str = typer.Option(
        "manual",
        "--mode",
        "-m",
        case_sensitive=False,
        help="Run mode: manual | auto. (auto-continue rounds are honoured by the ui channel.)",
    ),
    profile: str = typer.Option(
        "default",
        "--profile",
        "-p",
        case_sensitive=False,
        help="Agent profile: default | engineer | research. Selects AGENTS_*.md bootstrap.",
    ),
    max_tokens: int | None = typer.Option(
        None,
        "--max-tokens",
        help="Automation policy: stop auto loop when cumulative session tokens exceed this budget.",
    ),
    max_experiments: int | None = typer.Option(
        None,
        "--max-experiments",
        help="Automation policy: stop auto loop after N completed experiments.",
    ),
    project_dir: str | None = typer.Option(
        None,
        "--project-dir",
        help="Optional research project directory (forwarded as metadata.project_dir).",
    ),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="Render assistant output as Markdown"),
    logs: bool = typer.Option(False, "--logs/--no-logs", help="Show mira runtime logs during chat"),
    verbose: bool = typer.Option(False, "--verbose/--no-verbose", help="Show verbose runtime hints (including invoked skills)"),
    debug: bool = typer.Option(False, "--debug/--no-debug", help="Alias of --verbose"),
):
    """Interact with the research-flavoured agent (auto-mode, profiles, contracts)."""
    from mira_engine.agent.research_loop import ResearchAgentLoop
    from mira_engine.bus.queue import MessageBus
    from mira_engine.cron.service import CronService

    mode_value = (mode or "manual").strip().lower()
    if mode_value not in {"manual", "auto"}:
        console.print(
            f"[red]Invalid --mode value: {mode!r}. Expected one of: manual, auto.[/red]"
        )
        raise typer.Exit(1)
    profile_value = (profile or "default").strip().lower()
    if profile_value not in {"default", "engineer", "research"}:
        console.print(
            f"[red]Invalid --profile value: {profile!r}. "
            "Expected one of: default, engineer, research.[/red]"
        )
        raise typer.Exit(1)
    if max_tokens is not None and max_tokens <= 0:
        console.print("[red]--max-tokens must be a positive integer.[/red]")
        raise typer.Exit(1)
    if max_experiments is not None and max_experiments <= 0:
        console.print("[red]--max-experiments must be a positive integer.[/red]")
        raise typer.Exit(1)

    if workspace is None and sys.stdin.isatty():
        if typer.confirm("Do you want to use the current directory as a project workspace?"):
            workspace = os.getcwd()

    config = _load_runtime_config(config, workspace)

    sync_workspace_templates(config.workspace_path)

    bus = MessageBus()
    provider = _make_provider(config)
    model_router = ModelRouter(config.agents.defaults)

    cron_store_path = _workspace_cron_store(config)
    cron = CronService(cron_store_path)

    verbose_mode = verbose or debug
    logs_mode = logs or (debug and message is not None)
    _configure_cli_logging(logs_mode)

    agent_loop = ResearchAgentLoop(
        **_build_agent_loop_kwargs(
            bus=bus,
            provider=provider,
            config=config,
            cron_service=cron,
            model_router=model_router,
        ),
    )

    inbound_metadata = _build_research_inbound_metadata(
        mode=mode_value,
        profile=profile_value,
        max_tokens=max_tokens,
        max_experiments=max_experiments,
        project_dir=project_dir,
    )

    banner = (
        f"{__logo__} Research mode "
        f"(mode=[bold]{mode_value}[/bold], profile=[bold]{profile_value}[/bold]) "
        "(type [bold]exit[/bold], [bold]Ctrl+C[/bold] to interrupt, [bold]Ctrl+C x2[/bold] at prompt to quit)\n"
    )
    _run_cli_agent_session(
        agent_loop=agent_loop,
        bus=bus,
        message=message,
        session_id=session_id,
        markdown=markdown,
        verbose_mode=verbose_mode,
        logs_mode=logs_mode,
        inbound_metadata=inbound_metadata,
        interactive_banner=banner,
        model_name=config.agents.defaults.primary_model,
        provider_name=config.agents.defaults.provider,
    )


# ============================================================================
# Runtime Commands (Python environment management)
# ============================================================================


runtime_app = typer.Typer(help="Manage the per-project Python runtime")
app.add_typer(runtime_app, name="runtime")


@runtime_app.command("install-python")
def runtime_install_python(
    version: str | None = typer.Option(
        None,
        "--version",
        help="Python version to install (default: tools.exec.python.python_version from config)",
    ),
    config: str | None = typer.Option(None, "--config", help="Path to config.json"),
    workspace: str | None = typer.Option(None, "--workspace", help="Workspace path"),
):
    """Install the pinned CPython interpreter via ``uv python install``.

    Intended to run once at first launch (e.g. by the desktop installer)
    so that subsequent ``uv venv --python <ver>`` calls hit a warm cache
    rather than blocking on a network download. Idempotent — safe to
    re-run any time.
    """
    from mira_engine.runtime.python_env import (
        PythonEnvError,
        detect_uv,
        ensure_python_interpreter,
    )

    cfg = _load_runtime_config(config, workspace)
    python_cfg = cfg.tools.exec.python
    target = version or python_cfg.python_version

    if not target:
        console.print(
            "[red]No Python version specified.[/red] "
            "Set ``tools.exec.python.python_version`` in your config "
            "or pass ``--version 3.11``."
        )
        raise typer.Exit(code=2)

    binary = detect_uv()
    if binary is None:
        console.print(
            "[red]uv not found.[/red] Install it from "
            "https://docs.astral.sh/uv/ or rebuild the desktop bundle."
        )
        raise typer.Exit(code=1)

    console.print(f"Using uv at [cyan]{binary.path}[/cyan] (version "
                  f"{'.'.join(map(str, binary.version))})")
    console.print(f"Ensuring Python [cyan]{target}[/cyan] is installed...")

    try:
        ensure_python_interpreter(binary, target)
    except PythonEnvError as exc:
        console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]✓[/green] Python {target} ready.")


@runtime_app.command("info")
def runtime_info(
    config: str | None = typer.Option(None, "--config", help="Path to config.json"),
    workspace: str | None = typer.Option(None, "--workspace", help="Workspace path"),
):
    """Show the active Python runtime configuration and detected uv."""
    from mira_engine.runtime.python_env import detect_uv

    cfg = _load_runtime_config(config, workspace)
    python_cfg = cfg.tools.exec.python

    console.print(f"[bold]Manager:[/bold] {python_cfg.manager}")
    if python_cfg.manager == "off":
        console.print("[dim]Per-project venvs are disabled. "
                      "Set tools.exec.python.manager = 'uv' to enable.[/dim]")
        return

    console.print(f"[bold]Auto-bootstrap:[/bold] {python_cfg.auto_bootstrap}")
    console.print(f"[bold]Venv dir:[/bold] {python_cfg.venv_dir}")
    if python_cfg.python_version:
        console.print(f"[bold]Pinned python:[/bold] {python_cfg.python_version}")
    if python_cfg.cache_dir:
        console.print(f"[bold]uv cache dir:[/bold] {python_cfg.cache_dir}")
    if python_cfg.link_mode:
        console.print(f"[bold]Link mode:[/bold] {python_cfg.link_mode}")
    if python_cfg.baseline_requirements:
        console.print(
            "[bold]Baseline:[/bold] "
            + ", ".join(python_cfg.baseline_requirements)
        )

    binary = detect_uv()
    if binary is None:
        console.print("[red]uv:[/red] not found on PATH or in bundle")
    else:
        version = ".".join(map(str, binary.version))
        console.print(f"[green]uv:[/green] {binary.path} (v{version})")


def _human_size(num_bytes: int) -> str:
    """Format bytes like ``1.2 GiB`` for display."""
    step = 1024.0
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(num_bytes)
    for unit in units:
        if size < step or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= step
    return f"{size:.1f} TiB"


@runtime_app.command("cache-prune")
def runtime_cache_prune(
    dry_run: bool = typer.Option(
        False, "--dry-run/--apply",
        help="Preview what would be removed without deleting anything",
    ),
    config: str | None = typer.Option(None, "--config", help="Path to config.json"),
    workspace: str | None = typer.Option(None, "--workspace", help="Workspace path"),
):
    """Remove unreferenced packages from uv's global cache.

    Wraps ``uv cache prune``. Hardlink semantics mean a pruned package
    is only really freed if no project venv still pins it; the byte
    count uv reports is the headline savings.
    """
    from mira_engine.runtime.python_env import (
        PythonEnvError,
        detect_uv,
        prune_uv_cache,
    )

    cfg = _load_runtime_config(config, workspace)
    python_cfg = cfg.tools.exec.python

    binary = detect_uv()
    if binary is None:
        console.print("[red]uv not found.[/red]")
        raise typer.Exit(code=1)

    label = "Dry run:" if dry_run else "Pruning"
    console.print(f"{label} uv cache via {binary.path}...")
    try:
        output = prune_uv_cache(
            binary, cache_dir=python_cfg.cache_dir or None, dry_run=dry_run
        )
    except PythonEnvError as exc:
        console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if output:
        console.print(output)
    console.print("[green]✓[/green] cache prune complete.")


@runtime_app.command("project-gc")
def runtime_project_gc(
    root: str | None = typer.Option(
        None,
        "--root",
        help="Directory to scan (default: current workspace)",
    ),
    stale_days: int = typer.Option(
        30,
        "--stale-days",
        help="Project is 'stale' if no file outside the venv has been "
             "touched in this many days",
    ),
    delete_stale: bool = typer.Option(
        False,
        "--delete-stale",
        help="Delete venvs whose project hasn't been touched in --stale-days",
    ),
    delete: list[str] | None = typer.Option(
        None,
        "--delete",
        help="Delete a specific venv path (may be passed multiple times)",
    ),
    config: str | None = typer.Option(None, "--config", help="Path to config.json"),
    workspace: str | None = typer.Option(None, "--workspace", help="Workspace path"),
):
    """List (or delete) per-project ``.venv`` directories under a root.

    By default just prints a table of (size, last-used, project) so the
    user can decide what to clean up. Pass ``--delete-stale`` to remove
    every venv whose parent project has been idle for more than
    ``--stale-days`` days, or ``--delete <path>`` for surgical removal.
    """
    import time

    from mira_engine.runtime.python_env import (
        find_project_venvs,
        remove_venv,
    )

    cfg = _load_runtime_config(config, workspace)
    python_cfg = cfg.tools.exec.python
    venv_name = Path(python_cfg.venv_dir).name or ".venv"

    scan_root = Path(root).expanduser() if root else cfg.workspace_path
    console.print(f"Scanning [cyan]{scan_root}[/cyan] for ``{venv_name}`` directories...")

    venvs = find_project_venvs(scan_root, venv_dir_name=venv_name)
    if not venvs:
        console.print("[dim]no venvs found.[/dim]")
        return

    now = time.time()
    stale_cutoff = now - stale_days * 86400

    table = Table(title=f"Project venvs under {scan_root}")
    table.add_column("Size", justify="right")
    table.add_column("Last used")
    table.add_column("Project last touched")
    table.add_column("Project")
    table.add_column("Status")

    total = 0
    stale: list[Path] = []
    for info in venvs:
        total += info.size_bytes
        is_stale = info.last_project_activity < stale_cutoff
        if is_stale:
            stale.append(info.venv_path)
        last_used = (
            f"{int((now - info.last_used) / 86400)}d ago"
            if info.last_used
            else "?"
        )
        last_act = (
            f"{int((now - info.last_project_activity) / 86400)}d ago"
            if info.last_project_activity
            else "?"
        )
        table.add_row(
            _human_size(info.size_bytes),
            last_used,
            last_act,
            str(info.project_dir),
            "[yellow]stale[/yellow]" if is_stale else "[green]active[/green]",
        )
    console.print(table)
    console.print(
        f"Total: {_human_size(total)} across {len(venvs)} venv"
        f"{'s' if len(venvs) != 1 else ''}"
        + (f" ({len(stale)} stale)" if stale else "")
    )

    explicit = [Path(p).expanduser().resolve() for p in (delete or [])]
    targets: list[Path] = list(explicit)
    if delete_stale:
        targets.extend(stale)
    targets = list(dict.fromkeys(targets))

    if not targets:
        return

    freed = 0
    for venv in targets:
        try:
            freed += remove_venv(venv)
            console.print(f"[green]removed[/green] {venv}")
        except OSError as exc:
            console.print(f"[red]failed to remove[/red] {venv}: {exc}")
    console.print(f"Reclaimed [bold]{_human_size(freed)}[/bold] (apparent size).")


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
    from mira_engine.channels.registry import discover_all
    from mira_engine.config.loader import load_config, set_config_path

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
    names.update(("telegram", "whatsapp", "discord", "feishu", "mochat", "dingtalk", "email", "slack", "qq", "matrix", "ui"))

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
    from mira_engine.config.paths import get_bridge_install_dir

    user_bridge = get_bridge_install_dir()

    # Check if already built
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    # Check for npm
    if not shutil.which("npm"):
        console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)

    # Find source bridge: first check package data, then source dir
    pkg_bridge = Path(__file__).parent.parent / "bridge"  # mira/bridge (installed)
    src_bridge = Path(__file__).parent.parent.parent / "bridge"  # repo root/bridge (dev)

    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge

    if not source:
        console.print("[red]Bridge source not found.[/red]")
        console.print("Try reinstalling: pip install --force-reinstall mira")
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

    from mira_engine.bus.queue import MessageBus
    from mira_engine.channels.registry import discover_all
    from mira_engine.config.loader import load_config, set_config_path

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
    if channel == "ui":
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
    """Show mira status."""
    from mira_engine.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} mira Status\n")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

    if config_path.exists():
        from mira_engine.providers.registry import PROVIDERS

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
# OAuth Login (used by onboarding)
# ============================================================================


def _login_openai_codex() -> None:
    ensure_oauth_state_dirs_for_runtime()
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


def _login_github_copilot() -> None:
    ensure_oauth_state_dirs_for_runtime()
    console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")
    try:
        from mira_engine.providers.github_copilot_provider import login_github_copilot

        login_github_copilot(print_fn=lambda s: console.print(s))
        console.print("[green]✓ Authenticated with GitHub Copilot[/green]")
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1)


_LOGIN_HANDLERS: dict[str, callable] = {
    "openai_codex": _login_openai_codex,
    "github_copilot": _login_github_copilot,
}


def _run_oauth_login(provider_name: str) -> None:
    from mira_engine.providers.registry import find_by_name

    spec = find_by_name(provider_name)
    if not spec or not spec.is_oauth:
        raise typer.Exit(1)
    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]OAuth login not implemented for {spec.label}[/red]")
        raise typer.Exit(1)
    console.print(f"\n{__logo__} OAuth Login - {spec.label}\n")
    handler()


if __name__ == "__main__":
    app()
