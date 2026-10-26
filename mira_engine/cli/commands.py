"""CLI commands for mira."""

import asyncio
import json
import os
import select
import signal
import socket
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
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
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document as PtDocument
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from loguru import logger

from mira_engine import __logo__, __version__
from mira_engine.cli.plugin import plugin_app
from mira_engine.cli.remote import remote_app
from mira_engine.cli.workflow import workflow_app
from mira_engine.config.paths import get_workspace_path
from mira_engine.config.schema import Config
from mira_engine.providers.factory import make_provider, make_role_provider
from mira_engine.utils.helpers import sync_workspace_templates

app = typer.Typer(
    name="mira",
    help=f"{__logo__} mira - Personal AI Assistant",
    no_args_is_help=True,
)
app.add_typer(workflow_app, name="workflow")
app.add_typer(remote_app, name="remote")
app.add_typer(plugin_app, name="plugin")

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
    # (Providers from plugins declare theirs in plugin.json: `endpoint`.)
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
    "kimi_code": ("kimi-for-coding", "k3"),
    "zhipu_coding_plan": ("glm-5.3",),
    "minimax_token_plan": ("MiniMax-M3",),
    "volcengine_agent_plan": ("ark-code-latest",),
    "xai": ("grok-4.5",),
    "mistral": ("mistral-large-latest", "ministral-8b-latest"),
    "groq": ("llama-3.3-70b-versatile", "mixtral-8x7b-32768"),
    "ollama": ("llama3.2", "qwen2.5"),
    "vllm": ("meta-llama/Llama-3.1-8B-Instruct", "Qwen/Qwen2.5-7B-Instruct"),
    "ovms": ("meta-llama/Llama-3.1-8B-Instruct",),
    # (Providers from plugins declare theirs in plugin.json: `model_examples`.)
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
    if spec.name in _PROVIDER_DEFAULT_ENDPOINTS:
        return _PROVIDER_DEFAULT_ENDPOINTS[spec.name]
    from mira_engine.providers.registry import plugin_provider

    plugin = plugin_provider(spec.name)
    return plugin.endpoint if plugin else None


def _validate_model_input(model: str) -> str | None:
    """Validate model input and return normalized model or None."""
    value = model.strip()
    if not value:
        return None
    if any(ch.isspace() for ch in value):
        return None
    return value


def _provider_model_examples(provider_name: str) -> tuple[str, ...]:
    if provider_name in _PROVIDER_MODEL_EXAMPLES:
        return _PROVIDER_MODEL_EXAMPLES[provider_name]
    from mira_engine.providers.registry import plugin_provider

    plugin = plugin_provider(provider_name)
    return (plugin.model_examples if plugin else ()) or ("<provider-model-name>",)


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


def _load_models_command_config(config: str | None) -> tuple[Config, Path]:
    """Load config for model cache commands and return config plus path."""
    from mira_engine.config.loader import get_config_path, load_config, set_config_path

    config_path = Path(config).expanduser().resolve() if config else get_config_path()
    if config:
        set_config_path(config_path)
    return load_config(config_path), config_path


def _resolve_models_provider(config: Config, provider: str | None) -> str:
    """Resolve provider argument for model cache commands."""
    from mira_engine.providers.model_fetch import current_provider_name
    from mira_engine.providers.registry import find_by_name

    value = (provider or "").strip().replace("-", "_")
    if not value:
        value = current_provider_name(config) or ""
    spec = find_by_name(value) if value else None
    if not spec:
        raise typer.BadParameter(
            "Provider is not configured. Pass a provider name, or set agents.defaults.provider."
        )
    return spec.name


def _model_cache_table(caches) -> Table:
    """Render model caches as a compact table."""
    from mira_engine.providers.model_fetch import is_cache_stale

    table = Table(title="Cached Models")
    table.add_column("Provider")
    table.add_column("Models", justify="right")
    table.add_column("Fetched")
    table.add_column("Status")
    table.add_column("API Base")
    for cache in caches:
        status = "stale" if is_cache_stale(cache) else "fresh"
        table.add_row(
            cache.provider,
            str(len(cache.models)),
            cache.fetched_at or "-",
            status,
            cache.api_base or "-",
        )
    return table


def _find_cached_model(cache, model: str):
    """Find a model by native or config model id."""
    target = model.strip()
    for item in cache.models:
        if target in {item.id, item.config_model}:
            return item
    return None


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
    exit_armed_until: float,
    now: float,
) -> str:
    """Classify Ctrl+C at the ``You:`` prompt (ignore / hint / exit).

    ``exit_armed_until`` is set only after the user sees the quit hint at the
    prompt, so a Ctrl+C that interrupted an agent turn cannot be mistaken for
    a double-press exit.
    """
    if not turn_done_set:
        return PROMPT_CTRL_C_IGNORE
    if exit_armed_until and now < exit_armed_until:
        return PROMPT_CTRL_C_EXIT
    return PROMPT_CTRL_C_SHOW_HINT


def should_cancel_turn_on_sigint(*, turn_done_set: bool) -> bool:
    """Return True when SIGINT should cancel the in-flight agent turn."""
    return not turn_done_set


def handle_cli_loop_sigint(
    *,
    turn_done: asyncio.Event | None,
    interrupt_evt: asyncio.Event | None,
    exit_armed_until: list[float],
) -> str:
    """Handle SIGINT delivered via ``loop.add_signal_handler``.

    Returns ``turn_interrupt`` when an in-flight agent turn should stop, else
    ``prompt`` (caller should raise or delegate to prompt_toolkit).
    """
    at_prompt = turn_done is None or turn_done.is_set()
    if should_cancel_turn_on_sigint(turn_done_set=at_prompt):
        exit_armed_until[0] = 0.0
        if interrupt_evt is not None:
            interrupt_evt.set()
        return "turn_interrupt"
    return "prompt"


def install_cli_loop_sigint_handler(
    loop: asyncio.AbstractEventLoop,
    handler: object,
) -> bool:
    """Install a SIGINT handler on the running event loop. Returns False on unsupported platforms."""
    try:
        loop.add_signal_handler(signal.SIGINT, handler)  # type: ignore[arg-type]
        return True
    except (NotImplementedError, RuntimeError):
        return False


def remove_cli_loop_sigint_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Remove the loop SIGINT handler if present."""
    try:
        loop.remove_signal_handler(signal.SIGINT)
    except (NotImplementedError, RuntimeError):
        pass


async def wait_cli_turn_or_interrupt(
    *,
    turn_done: asyncio.Event,
    interrupt_evt: asyncio.Event,
) -> bool:
    """Wait for turn completion or a turn-interrupt signal.

    Returns True when ``interrupt_evt`` fired (Ctrl+C during agent work).
    """
    wait_turn = asyncio.create_task(turn_done.wait())
    wait_intr = asyncio.create_task(interrupt_evt.wait())
    done, pending = await asyncio.wait(
        {wait_turn, wait_intr},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return interrupt_evt.is_set()


async def interrupt_cli_agent_turn(
    *,
    turn_done: asyncio.Event,
    turn_response: list[str],
    turn_skills: set[str],
    dispatch_tasks: list[asyncio.Task],
    cancel_subagents: object | None = None,
    on_interrupted: object | None = None,
    user_interrupted: bool = False,
    cancel_timeout: float = 1.0,
) -> None:
    """Stop the current CLI turn: unblock the prompt and cancel dispatch tasks."""
    if not turn_done.is_set() or user_interrupted:
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


# Directories never suggested in file-path completions.
_COMPLETER_IGNORE_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".coverage",
    "htmlcov",
}


class MiraCompleter(Completer):
    """File-path completer triggered by @ in the CLI prompt."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self._file_cache: list[tuple[str, Path]] | None = None

    def _scan_files(self) -> list[str]:
        """Scan workspace once and return sorted relative paths as strings."""
        if self._file_cache is not None:
            return [p for p, _ in self._file_cache]
        entries: list[tuple[str, Path]] = []
        for f in self.workspace.rglob("*"):
            if not f.is_file() or f.name.startswith("."):
                continue
            rel = f.relative_to(self.workspace)
            if any(
                part.startswith(".") or part in _COMPLETER_IGNORE_DIRS
                for part in rel.parts
            ):
                continue
            # Emit POSIX-style separators so completions are consistent across
            # platforms (Windows ``str(rel)`` would yield backslashes, breaking
            # "@dir/file" matching and downstream "/"-style path references).
            entries.append((rel.as_posix(), f))
        entries.sort(key=lambda e: e[0])
        self._file_cache = entries
        return [p for p, _ in entries]

    def get_completions(
        self, document: PtDocument, complete_event: object | None
    ) -> list[Completion]:
        text = document.text_before_cursor
        at_pos = text.rfind("@")
        if at_pos == -1:
            return []
        raw_partial = text[at_pos + 1:]
        filtered_partial = raw_partial.strip()
        yielded = 0
        for rel_str in self._scan_files():
            if filtered_partial and filtered_partial not in rel_str:
                continue
            yield Completion(rel_str, -(len(raw_partial) + 1))
            yielded += 1
            if yielded >= 50:
                break


def _init_prompt_session(workspace: Path | None = None) -> None:
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

    ws = workspace or Path.cwd()
    completer = MiraCompleter(ws)

    # Custom key bindings
    kb = KeyBindings()

    @kb.add("@")
    def _(event):
        """Insert @ then show file completion menu immediately."""
        b = event.current_buffer
        b.insert_text("@")
        b.start_completion(insert_common_part=False)

    @kb.add(Keys.Enter)
    def _(event):
        b = event.current_buffer
        cs = b.complete_state
        if cs is not None and len(cs.completions) > 0:
            # Select first completion if none selected yet
            completion = cs.current_completion
            if completion is None:
                b.complete_next()
                completion = cs.current_completion
            if completion is not None:
                b.apply_completion(completion)
            return
        # Default behavior: submit
        b.validate_and_handle()

    def _refresh_completion(buffer):
        """Buffer text changed — refresh completion if after @."""
        text = buffer.text
        at_pos = text.rfind("@")
        if at_pos == -1:
            return
        # Cancel existing completion menu and restart so list updates
        if buffer.complete_state:
            buffer.cancel_completion()
        buffer.start_completion(insert_common_part=False)

    _PROMPT_SESSION = PromptSession(
        history=SafeFileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,   # Enter submits (single line mode)
        complete_while_typing=False,
        completer=completer,
        key_bindings=kb,
    )
    _PROMPT_SESSION.default_buffer.on_text_changed += _refresh_completion


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
        raise KeyboardInterrupt



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
    from mira_engine.cli.launcher import ensure_launcher

    # Keeps <MIRA_HOME>/bin/mira pointing at a working install for Remote-SSH.
    ensure_launcher()


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


def _run_profile_prompts(cfg, config_path: Path, *, force: bool = False) -> bool:
    """Interactive first-run profile setup (USER.md + SOUL.md).

    Prompts for the user's name + preferred language(s) and the agent's name
    (+ optional identity), then writes the files and syncs timezone/language
    into config via the shared :mod:`mira_engine.profile` module. Timezone is
    auto-detected, never prompted. Returns True when a profile was saved.
    """
    from mira_engine.profile import apply_profile, detect_timezone, get_profile_state

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False

    state = get_profile_state(cfg)
    if not force and not state.get("needs_onboarding"):
        return False

    cur_user = state.get("user", {})
    cur_agent = state.get("agent", {})

    console.print(f"\n{__logo__} Let's set up your MIRA profile.")
    console.print("[dim]This fills USER.md (about you) and SOUL.md (your agent's identity).[/dim]\n")

    user_name = typer.prompt(
        "Your name", default=cur_user.get("name") or "MIRA User"
    ).strip()
    languages = typer.prompt(
        "Preferred language(s)", default=cur_user.get("languages") or "English"
    ).strip()

    agent_name = typer.prompt(
        "Agent name", default=cur_agent.get("name") or "mira 🐈"
    ).strip()
    agent_identity = typer.prompt(
        "Agent identity (one line, optional)",
        default=cur_agent.get("identity") or "",
        show_default=bool(cur_agent.get("identity")),
    ).strip()

    timezone = detect_timezone()
    payload = {
        "user": {"name": user_name, "timezone": timezone, "languages": languages},
        "agent": {"name": agent_name, "identity": agent_identity},
    }
    try:
        apply_profile(cfg, payload, config_path=config_path)
    except ValueError as exc:
        console.print(f"[red]Profile not saved: {exc}[/red]")
        return False

    console.print(
        f"[green]✓[/green] Profile saved (timezone auto-detected: [cyan]{timezone}[/cyan])"
    )
    return True


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
    from mira_engine.providers.registry import all_providers, find_by_name

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
        specs = list(all_providers())
        if specs and typer.confirm("Configure a model provider now?", default=True):
            provider_rows: list[tuple[object, object | None, bool, str | None]] = []
            for spec in specs:
                provider_cfg = cfg.providers.entry(spec.name)
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
                    selected_cfg = cfg.providers.entry(selected.name, create=True)
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

    # First-run profile (USER.md / SOUL.md). Reload so the helper sees the
    # freshly created/saved config, then persist any profile back to disk.
    cfg = load_config(config_path)
    if _run_profile_prompts(cfg, config_path):
        cfg = load_config(config_path)

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


@app.command()
def profile(
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    force: bool = typer.Option(False, "--force", help="Re-run even if already onboarded"),
):
    """Set up your user (USER.md) and agent (SOUL.md) profile."""
    from mira_engine.config.loader import get_config_path, load_config, set_config_path

    config_path = Path(config).expanduser().resolve() if config else get_config_path()
    if config:
        set_config_path(config_path)
    if not config_path.exists():
        console.print("[yellow]No config found. Run `mira onboard` first.[/yellow]")
        raise typer.Exit(1)

    cfg = load_config(config_path)
    workspace_path = get_workspace_path(cfg.workspace_path)
    _ensure_workspace_bootstrap(workspace_path)
    cfg = load_config(config_path)

    if not _run_profile_prompts(cfg, config_path, force=force):
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            console.print("[yellow]Profile setup needs an interactive terminal.[/yellow]")
        else:
            console.print(
                "[dim]Profile already set up. Use --force to edit it again.[/dim]"
            )


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


def _make_gateway_provider(config: Config, model: str | None = None):
    """Create a provider for the long-running gateway.

    Unlike the one-shot CLI helpers, the gateway must stay alive even when no
    provider is configured yet so the UI (Providers page / onboarding) can
    finish setup. When matching fails we fall back to the bundle-setup
    placeholder, which returns an actionable message on every request instead
    of crashing the gateway at boot.
    """
    from mira_engine.providers.factory import BundleSetupRequiredProvider

    try:
        return make_provider(config, model)
    except ValueError as exc:
        console.print(
            f"[yellow]Warning: {exc} Gateway will start unconfigured — open the "
            "Providers settings to add a provider.[/yellow]"
        )
        return BundleSetupRequiredProvider()


def _make_gateway_role_provider(config: Config, role: str):
    """Role provider factory for the gateway that tolerates an unconfigured setup."""
    from mira_engine.providers.factory import BundleSetupRequiredProvider

    defaults = config.agents.defaults
    try:
        return make_role_provider(config, role)
    except ValueError:
        model = defaults.role_model(role)
        candidates = tuple(defaults.role_model_candidates(role))
        return BundleSetupRequiredProvider(), model, candidates


def _routing_kwargs(config: Config) -> dict[str, object]:
    """Provider/model fallback + team role provider wiring for agent loops."""
    return dict(
        provider_factory=lambda model: _make_provider_for_model(config, model),
        model_candidates=config.agents.defaults.default_model_candidates,
        role_provider_factory=lambda role: make_role_provider(config, role),
    )


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


def _ui_enabled_in_config_file(config_path: Path | None) -> bool | None:
    """Return the explicit ``channels.ui.enabled`` from disk, or ``None`` if unset.

    We inspect the raw JSON instead of the parsed config so we can tell the
    difference between "user never configured the UI channel" (``None`` →
    gateway turns it on) and "user explicitly disabled it" (``False`` → leave
    it off). The legacy ``web`` alias is honored for older configs.
    """
    if not config_path or not config_path.exists():
        return None
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    channels = payload.get("channels")
    if not isinstance(channels, dict):
        return None
    for key in ("ui", "web"):
        section = channels.get(key)
        if isinstance(section, dict) and "enabled" in section:
            return bool(section["enabled"])
    return None


def _resolve_gateway_ui_enabled(explicit: bool | None, no_ui: bool) -> bool:
    """Decide whether the gateway should run the local control-plane UI channel.

    ``--no-ui`` always wins (headless/messaging-only deployments). Otherwise an
    explicit on-disk value is respected, and an unset value defaults the UI on
    so the desktop/browser app and the Providers onboarding page work without a
    prior ``mira onboard``.
    """
    if no_ui:
        return False
    if explicit is None:
        return True
    return explicit


def _prepare_gateway_ui_channel(config: Config, *, no_ui: bool) -> None:
    """Ensure the gateway exposes the UI channel and bootstraps a config file.

    The UI channel is the control plane for the desktop/browser client. Without
    it there is no way to reach onboarding or the Providers settings page, so the
    gateway turns it on by default (unless explicitly disabled or ``--no-ui``).
    On a fresh home with no ``config.json`` we also persist the defaults so the
    setup no longer depends on running ``mira onboard`` first.
    """
    from mira_engine.config.loader import get_config_path, save_config
    from mira_engine.config.schema import UiChannelConfig

    config_path = get_config_path()
    explicit = _ui_enabled_in_config_file(config_path)
    enabled = _resolve_gateway_ui_enabled(explicit, no_ui)

    # ``channels.ui`` is a typed model on a freshly built config, but a raw dict
    # when loaded from disk (ChannelsConfig keeps channels as extra fields).
    # Normalize to the typed model so downstream access and serialization are
    # consistent, then store it back in the channels extras.
    section = getattr(config.channels, "ui", None)
    if isinstance(section, UiChannelConfig):
        ui = section
    elif isinstance(section, dict):
        ui = UiChannelConfig.model_validate(section)
    else:
        ui = UiChannelConfig()
    ui.enabled = enabled
    # An enabled UI channel with an empty allowFrom denies everyone and aborts
    # startup; fall back to the local-control-plane default of "*".
    if ui.enabled and not ui.allow_from:
        ui.allow_from = ["*"]
    extras = config.channels.__pydantic_extra__
    if extras is None:
        extras = {}
        object.__setattr__(config.channels, "__pydantic_extra__", extras)
    extras["ui"] = ui

    if not config_path.exists():
        try:
            save_config(config, config_path)
        except OSError:
            # Non-fatal: the UI's own save path will create the file later.
            return
        console.print(
            f"[green]✓[/green] Created default config at {config_path} "
            "— open MIRA and use the Providers page to add a model provider."
        )


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
    from mira_engine.config.loader import get_home_dir
    from mira_engine.utils.locks import interprocess_lock

    # Serialize check + publish, not just each individual file write.
    with interprocess_lock(get_home_dir() / "runtime" / "gateway-reservation"):
        _reserve_gateway_endpoint(gateway_host, gateway_port, verbose)


def _reserve_gateway_endpoint(gateway_host: str, gateway_port: int, verbose: bool = False) -> None:
    """Reserve one gateway endpoint without blocking gateways on other ports."""
    import atexit
    import hashlib
    import os
    import socket

    import psutil

    if os.environ.get("MIRA_SKIP_GATEWAY_FAILSAVE"):
        return

    from mira_engine.cli.gateway_lifecycle import is_gateway_command
    from mira_engine.config.loader import get_config_path, get_home_dir
    from mira_engine.utils.locks import locked_update_text, locked_write_text

    runtime_dir = get_home_dir() / "runtime"
    endpoint = f"{gateway_host}:{gateway_port}"
    endpoint_key = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:16]
    pid_file = runtime_dir / "gateways" / f"{endpoint_key}.pid"
    discovery_file = runtime_dir / "gateways.json"
    pid_file.parent.mkdir(parents=True, exist_ok=True)

    # 1. Check only this endpoint's PID reservation. Other ports may coexist.
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            if psutil.pid_exists(old_pid):
                proc = psutil.Process(old_pid)
                record = None
                if discovery_file.exists():
                    try:
                        discovery = json.loads(discovery_file.read_text(encoding="utf-8"))
                        record = discovery.get("instances", {}).get(endpoint_key)
                    except (ValueError, AttributeError):
                        pass
                same_process = not isinstance(record, dict) or record.get("create_time") is None or (
                    record.get("pid") == old_pid and abs(proc.create_time() - record["create_time"]) < 0.01
                )
                if same_process and is_gateway_command(proc.cmdline()):
                    # Note: Using basic print here as it is before main gateway loop setup
                    print(f"错误: Mira 已经在运行中 (PID: {old_pid})。")
                    print("提示: 系统服务请使用 `mira-engine stop`；独立 gateway 请在相同 MIRA_HOME 下使用 `mira-engine stop --gateway`。")
                    raise typer.Exit(1)
        except typer.Exit:
            raise
        except psutil.AccessDenied:
            print("无法确认已有 PID 的归属；请检查权限，不会删除其运行记录。")
            raise typer.Exit(1)
        except (ValueError, psutil.NoSuchProcess):
            pid_file.unlink(missing_ok=True)

    # 2. Check the actual endpoint in case its owner did not create a PID file.
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

    # 3. Atomically publish this gateway for local discovery.
    current_pid = os.getpid()
    locked_write_text(pid_file, str(current_pid))

    def update_discovery(current_text: str) -> str:
        try:
            payload = json.loads(current_text) if current_text.strip() else {}
        except json.JSONDecodeError:
            payload = {}
        instances = payload.get("instances") if isinstance(payload, dict) else None
        if not isinstance(instances, dict):
            instances = {}
        instances[endpoint_key] = {
            "host": gateway_host,
            "port": gateway_port,
            "pid": current_pid,
            "endpoint": endpoint,
            "create_time": psutil.Process(current_pid).create_time(),
            "config_path": str(get_config_path().resolve()),
            "mira_home": str(get_home_dir().resolve()),
            "launch_mode": os.environ.get("MIRA_SERVICE_MODE", "standalone"),
        }
        return json.dumps({"instances": instances}, indent=2, ensure_ascii=False) + "\n"

    locked_update_text(discovery_file, update_discovery)

    def release_endpoint() -> None:
        try:
            if pid_file.exists() and pid_file.read_text().strip() == str(current_pid):
                pid_file.unlink(missing_ok=True)

            def remove_from_discovery(current_text: str) -> str:
                try:
                    payload = json.loads(current_text) if current_text.strip() else {}
                except json.JSONDecodeError:
                    payload = {}
                instances = payload.get("instances") if isinstance(payload, dict) else None
                if not isinstance(instances, dict):
                    instances = {}
                current = instances.get(endpoint_key)
                if isinstance(current, dict) and current.get("pid") == current_pid:
                    instances.pop(endpoint_key, None)
                return json.dumps({"instances": instances}, indent=2, ensure_ascii=False) + "\n"

            locked_update_text(discovery_file, remove_from_discovery)
        except OSError:
            pass

    atexit.register(release_endpoint)


@app.command()
def gateway(
    host: str | None = typer.Option(None, "--host", help="Gateway host"),
    port: int | None = typer.Option(None, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    no_ui: bool = typer.Option(
        False,
        "--no-ui",
        help="Do not start the UI channel (headless / messaging-only gateway)",
    ),
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
    from mira_engine.observability import configure_runtime_logging, emit_event

    diagnostics = configure_runtime_logging(mode="gateway")
    _prepare_gateway_ui_channel(config, no_ui=no_ui)

    if host is not None:
        config.gateway.host = host

    if port is not None:
        config.gateway.port = port

    gateway_host = config.gateway.host
    gateway_port = config.gateway.port

    _refuse_exposed_remote_engine(gateway_host)
    engine_lease = _acquire_engine_lease_or_exit(gateway_host, gateway_port)
    _free_gateway_port_or_exit(gateway_host, gateway_port)
    _gateway_failsafe_check(gateway_host, gateway_port, verbose)

    console.print(f"{__logo__} Starting mira gateway on {gateway_host}:{gateway_port}...")
    _sync_workspace_templates_or_exit(config.workspace_path)
    try:
        from mira_engine.profile import needs_onboarding

        if needs_onboarding(config):
            console.print(
                "[dim]Tip: run [cyan]mira profile[/cyan] (or use the desktop app) to set up "
                "your USER.md / SOUL.md profile.[/dim]"
            )
    except Exception:  # noqa: BLE001 - hint must never block startup
        pass
    bus = MessageBus()
    provider = _make_gateway_provider(config)
    provider_factory = lambda model: _make_gateway_provider(config, model)
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
        knowledge_config=config.tools.knowledge,
        cron_service=cron,
        timezone=default_tz,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        community_config=config.community,
        provider_factory=provider_factory,
        model_candidates=config.agents.defaults.default_model_candidates,
        role_provider_factory=lambda role: _make_gateway_role_provider(config, role),
        auto_max_rounds=config.agents.defaults.auto_max_rounds,
    )

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        from mira_engine.agent.tools.cron import CronTool
        from mira_engine.agent.tools.message import MessageTool
        from mira_engine.cron.system_jobs import run_system_job

        # Built-in jobs (Dream) run directly, not as an agent turn.
        if await run_system_job(job, lambda: agent.dream.run()):
            return None
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
            metadata = {
                key: value
                for key, value in {
                    "project_id": job.payload.project_id,
                    "project_dir": job.payload.project_dir,
                }.items()
                if value
            }
            response = await agent.process_direct(
                reminder_note,
                session_key=f"cron:{job.id}",
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
                metadata=metadata,
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
                content=response,
                metadata=metadata,
            ))
        return response
    cron.on_job = on_cron_job

    from mira_engine.cron.system_jobs import register_dream_job

    register_dream_job(cron, config.agents.defaults.dream, default_tz)

    from mira_engine.heartbeat.target import HeartbeatTarget, pick_heartbeat_target

    # The target of the tick being executed, so delivery goes where the turn
    # ran even if another session was touched in between.
    heartbeat_target: HeartbeatTarget | None = None

    def _heartbeat_target() -> HeartbeatTarget:
        """Where this tick's turn runs and where what it says goes — never the
        chat the user happens to be using (``heartbeat/target.py``)."""
        return pick_heartbeat_target(
            enabled_channels=channels.enabled_channels,
            recent_session_keys=(
                str(item.get("key") or "") for item in session_manager.list_sessions()
            ),
            deliver_to=config.gateway.heartbeat.deliver_to,
        )

    async def on_heartbeat_execute(tasks: str) -> Any:
        """Phase 2: run the tasks through the full agent loop, in the target's session."""
        nonlocal heartbeat_target
        heartbeat_target = target = _heartbeat_target()

        async def _silent(*_args, **_kwargs):
            pass

        metadata: dict[str, Any] = {"_background": "heartbeat"}
        if target.dedicated:
            # The same working directory a user's turn in that chat gets.
            try:
                from mira_engine.chats import ensure_chat_workdir

                metadata["chat_dir"] = str(ensure_chat_workdir(config.workspace_path, target.chat_id))
            except (OSError, ValueError) as exc:
                logger.debug("No working directory for the {} chat: {}", target.chat_id, exc)
        return await agent.process_direct(
            tasks,
            session_key=target.session_key,
            channel=target.channel,
            chat_id=target.chat_id,
            on_progress=_silent,
            metadata=metadata,
        )

    async def on_heartbeat_notify(response: str) -> None:
        """Deliver what the turn said where the turn ran."""
        from mira_engine.bus.events import OutboundMessage

        target = heartbeat_target or _heartbeat_target()
        if not target.deliverable:
            return  # no channel to deliver to; the turn's work stays in its session
        await bus.publish_outbound(OutboundMessage(
            channel=target.channel,
            chat_id=target.chat_id,
            content=response,
            metadata={"_background": "heartbeat"},
        ))

    async def heartbeat_should_notify(response: str, task_context: str) -> bool:
        """A chat dedicated to background work gets everything the turn said;
        someone's own conversation only what is worth interrupting them for."""
        target = heartbeat_target or _heartbeat_target()
        if target.dedicated:
            return True
        from mira_engine.utils import evaluator

        return await evaluator.evaluate_response(
            response=response,
            task_context=task_context,
            provider=heartbeat.provider,
            model=heartbeat.model,
        )

    async def gather_heartbeat_context() -> str:
        """Fold relevant community activity into the heartbeat decision."""
        from mira_engine.community.digest import gather_community_digest

        return await gather_community_digest(config.community)

    hb_cfg = config.gateway.heartbeat
    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        provider=provider,
        model=agent.model,
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
        extra_context=gather_heartbeat_context,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
        should_notify=heartbeat_should_notify,
    )

    async def on_ui_runtime_config_updated(next_config: Config, projects_root: Path) -> None:
        nonlocal config, provider, provider_factory, default_tz, session_manager

        next_provider = _make_gateway_provider(next_config)
        next_provider_factory = lambda model: _make_gateway_provider(next_config, model)
        next_tz = next_config.agents.defaults.timezone
        next_workspace = projects_root.expanduser()

        await agent.reconfigure_runtime(
            provider=next_provider,
            model=next_config.agents.defaults.primary_model,
            provider_factory=next_provider_factory,
            model_candidates=next_config.agents.defaults.default_model_candidates,
            role_provider_factory=lambda role: _make_gateway_role_provider(next_config, role),
            workspace=next_workspace,
            max_iterations=next_config.agents.defaults.max_tool_iterations,
            max_tokens=next_config.agents.defaults.max_tokens,
            reasoning_effort=next_config.agents.defaults.reasoning_effort,
            temperature=next_config.agents.defaults.temperature,
            auto_max_rounds=next_config.agents.defaults.auto_max_rounds,
            restrict_to_workspace=next_config.tools.restrict_to_workspace,
            brave_api_key=next_config.tools.web.search.api_key or None,
            web_proxy=next_config.tools.web.proxy or None,
            exec_config=next_config.tools.exec,
            knowledge_config=next_config.tools.knowledge,
            timezone=next_tz,
            channels_config=next_config.channels,
            community_config=next_config.community,
            context_window_tokens=next_config.agents.defaults.context_window_tokens,
        )

        heartbeat.provider = next_provider
        heartbeat.model = agent.model
        heartbeat.workspace = next_workspace
        heartbeat.interval_s = next_config.gateway.heartbeat.interval_s
        heartbeat.enabled = next_config.gateway.heartbeat.enabled
        session_manager = agent.sessions

        # Keep the live UI channel's workspace-access policy in sync so the
        # data-path visibility check reflects the latest setting without a
        # gateway restart.
        ui_channel = channels.channels.get("ui")
        if ui_channel is not None and hasattr(ui_channel, "restrict_to_workspace"):
            ui_channel.restrict_to_workspace = next_config.tools.restrict_to_workspace

        config = next_config
        provider = next_provider
        provider_factory = next_provider_factory
        default_tz = next_tz
        register_dream_job(cron, next_config.agents.defaults.dream, next_tz)
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

    from mira_engine.cli.gateway_shutdown import GatewayShutdown, run_bounded
    from mira_engine.plugins.remote import poller_from_config

    # Remote kill switch for plugin versions (mira#210).
    plugin_poller = poller_from_config(config)

    shutdown = GatewayShutdown(on_force_exit=engine_lease.release)

    async def run():
        loop = asyncio.get_running_loop()
        if diagnostics is not None:
            diagnostics.install_asyncio_handler(loop)
        stop_requested = shutdown.bind(loop)
        emit_event("gateway.starting", host=gateway_host, port=gateway_port)
        main: asyncio.Future | None = None
        stop_wait = asyncio.create_task(stop_requested.wait())
        try:
            await cron.start()
            await heartbeat.start()
            if plugin_poller is not None:
                plugin_poller.start()
            main = asyncio.gather(agent.run(), channels.start_all())
            done, _ = await asyncio.wait({main, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
            if main in done:
                main.result()
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        except Exception as exc:
            emit_event("gateway.failed", level="CRITICAL", exception=exc)
            raise
        finally:
            stop_wait.cancel()
            # Leave a margin inside the watchdog budget for asyncio's own teardown.
            deadline = loop.time() + max(1.0, shutdown.timeout_s - 2.0)
            # Stop accepting connections and close WebSockets first, so no new work arrives.
            await run_bounded("channels", channels.stop_all(), deadline)
            heartbeat.stop()
            cron.stop()
            if plugin_poller is not None:
                plugin_poller.stop()
            agent.stop()
            await run_bounded("MCP servers", agent.close_mcp(), deadline)
            if main is not None and not main.done():
                main.cancel()
                await run_bounded("agent loop", asyncio.gather(main, return_exceptions=True), deadline)
            engine_lease.release()
            emit_event("gateway.stopped", signal=shutdown.signum)

    shutdown.install()
    try:
        asyncio.run(run())
    finally:
        shutdown.restore()
        engine_lease.release()


def _refuse_exposed_remote_engine(gateway_host: str) -> None:
    """Remote-SSH engines are reached through an SSH tunnel and must never listen publicly."""
    from mira_engine.cli.remote import LAUNCH_MODE_ENV, REMOTE_LAUNCH_MODE
    from mira_engine.security.gateway_auth import is_loopback_host

    if os.environ.get(LAUNCH_MODE_ENV) == REMOTE_LAUNCH_MODE and not is_loopback_host(gateway_host):
        typer.echo(f"Error: a Remote-SSH engine must bind a loopback address, not {gateway_host}.", err=True)
        raise typer.Exit(2)


def _acquire_engine_lease_or_exit(gateway_host: str, gateway_port: int):
    """Take the single-engine lease for MIRA_HOME before touching its data."""
    import atexit

    from mira_engine.cli.engine_lease import (
        LEASE_CONFLICT_EXIT_CODE,
        LeaseConflictError,
        acquire_engine_lease,
    )

    try:
        lease = acquire_engine_lease(host=gateway_host, port=gateway_port)
    except LeaseConflictError as exc:
        typer.echo(f"Error: {exc}.", err=True)
        typer.echo("Inspect it with `mira remote status`. Stop it with `mira remote stop`, "
                   "or `mira-engine stop` for the system service.", err=True)
        raise typer.Exit(LEASE_CONFLICT_EXIT_CODE) from None
    # Covers exits before the event loop starts; release() is idempotent.
    atexit.register(lease.release)
    return lease


def _free_gateway_port_or_exit(gateway_host: str, gateway_port: int) -> None:
    """Stop only a provably stale engine for this MIRA_HOME on our port; refuse anything else."""
    from mira_engine.cli.port_guard import (
        PORT_IN_USE_EXIT_CODE,
        PortInUseError,
        free_port_from_stale_engine,
    )

    try:
        free_port_from_stale_engine(gateway_host, gateway_port)
    except PortInUseError as exc:
        typer.echo(f"Error: {exc}.", err=True)
        typer.echo("Mira only stops a leftover engine for this MIRA_HOME. Stop that process, "
                   "or start the gateway on another port with --port.", err=True)
        raise typer.Exit(PORT_IN_USE_EXIT_CODE) from None


@app.command("gateway-token")
def gateway_token(
    rotate: bool = typer.Option(False, "--rotate", help="Generate a new token file (restart the gateway to apply)"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Print the UI gateway auth token (created on first use)."""
    from mira_engine.config.loader import load_config
    from mira_engine.security.gateway_auth import (
        TOKEN_ENV,
        ensure_token_file,
        gateway_token_path,
        resolve_gateway_token,
    )

    config_path = Path(config).expanduser().resolve() if config else None
    if config_path is not None and not config_path.exists():
        typer.echo(f"Error: Config file not found: {config_path}", err=True)
        raise typer.Exit(1)
    configured = load_config(config_path).gateway.auth_token
    if rotate:
        ensure_token_file(gateway_token_path(), rotate=True)
        if os.environ.get(TOKEN_ENV, "").strip() or configured.strip():
            typer.echo(f"Warning: {TOKEN_ENV} or gateway.authToken overrides the rotated token file.",
                       err=True)
    # stdout carries only the token so scripts can capture it.
    typer.echo(resolve_gateway_token(configured))


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
    from mira_engine.api.server import create_app, warn_if_exposed
    from mira_engine.bus.queue import MessageBus
    from mira_engine.security.api_auth import api_allowed_origins, resolve_api_keys
    from mira_engine.session.manager import SessionManager

    cfg = _load_runtime_config(config, workspace)
    from mira_engine.observability import configure_runtime_logging

    diagnostics = configure_runtime_logging(mode="api")
    sync_workspace_templates(cfg.workspace_path)

    provider = _make_provider(cfg)
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
        knowledge_config=cfg.tools.knowledge,
        timezone=default_tz,
        restrict_to_workspace=cfg.tools.restrict_to_workspace,
        session_manager=SessionManager(cfg.workspace_path),
        mcp_servers=cfg.tools.mcp_servers,
        channels_config=cfg.channels,
        provider_factory=lambda model: _make_provider_for_model(cfg, model),
        model_candidates=cfg.agents.defaults.default_model_candidates,
        role_provider_factory=lambda role: make_role_provider(cfg, role),
        auto_max_rounds=cfg.agents.defaults.auto_max_rounds,
    )

    api_host = host if host is not None else cfg.api.host
    api_port = port if port is not None else cfg.api.port
    request_timeout = timeout if timeout is not None else cfg.api.timeout
    api_keys, key_source = resolve_api_keys(cfg.api.keys, cfg.gateway.auth_token)
    origins = api_allowed_origins(cfg.api.cors_origins)
    logger.info("API server accepts {} key(s) from {}", len(api_keys), key_source)
    warn_if_exposed(api_host, origins)
    api_app = create_app(
        agent_loop=agent_loop,
        model_name=cfg.agents.defaults.primary_model,
        request_timeout=request_timeout,
        api_keys=api_keys,
        allowed_origins=origins,
    )
    if diagnostics is not None:
        async def configure_api_diagnostics(_app):
            diagnostics.install_asyncio_handler(asyncio.get_running_loop())
        api_app.on_startup.append(configure_api_diagnostics)
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
        knowledge_config=config.tools.knowledge,
        cron_service=cron_service,
        timezone=default_tz,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        provider_factory=lambda model: _make_provider_for_model(config, model),
        model_candidates=config.agents.defaults.default_model_candidates,
        role_provider_factory=lambda role: make_role_provider(config, role),
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
    workspace: Path | None = None,
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
    _init_prompt_session(workspace)
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
    _exit_armed_until = [0.0]
    _cli_session_key = f"{cli_channel}:{cli_chat_id}"
    _turn_done_ref: list[asyncio.Event | None] = [None]
    _loop_ref: list[asyncio.AbstractEventLoop | None] = [None]
    _cancel_turn_ref: list = [None]
    _shutdown_ref: list = [None]
    _turn_interrupt_evt_ref: list[asyncio.Event | None] = [None]
    _loop_sigint_installed = [False]

    class _CliSessionExit(Exception):
        """Raised to end the interactive loop without sys.exit() in a signal handler."""

    def _schedule_on_loop(coro_factory) -> None:
        loop = _loop_ref[0]
        if loop is None:
            return
        loop.call_soon_threadsafe(lambda: loop.create_task(coro_factory()))

    def _on_cli_loop_sigint() -> None:
        """SIGINT callback registered with ``loop.add_signal_handler`` (turn wait phase)."""
        action = handle_cli_loop_sigint(
            turn_done=_turn_done_ref[0],
            interrupt_evt=_turn_interrupt_evt_ref[0],
            exit_armed_until=_exit_armed_until,
        )
        if action == "turn_interrupt":
            cancel_turn = _cancel_turn_ref[0]
            if cancel_turn is not None:
                _schedule_on_loop(cancel_turn)
            return
        raise KeyboardInterrupt

    def _handle_sigint_fallback(signum, frame):
        """POSIX fallback when ``add_signal_handler`` is unavailable."""
        if (
            handle_cli_loop_sigint(
                turn_done=_turn_done_ref[0],
                interrupt_evt=_turn_interrupt_evt_ref[0],
                exit_armed_until=_exit_armed_until,
            )
            == "turn_interrupt"
        ):
            cancel_turn = _cancel_turn_ref[0]
            if cancel_turn is not None:
                _schedule_on_loop(cancel_turn)
            return
        raise KeyboardInterrupt

    def _install_loop_sigint() -> None:
        loop = _loop_ref[0]
        if loop is None or _loop_sigint_installed[0]:
            return
        if install_cli_loop_sigint_handler(loop, _on_cli_loop_sigint):
            _loop_sigint_installed[0] = True
        else:
            signal.signal(signal.SIGINT, _handle_sigint_fallback)

    def _uninstall_loop_sigint() -> None:
        loop = _loop_ref[0]
        if loop is None or not _loop_sigint_installed[0]:
            return
        remove_cli_loop_sigint_handler(loop)
        _loop_sigint_installed[0] = False

    def _handle_term(signum, frame):
        shutdown = _shutdown_ref[0]
        if shutdown is not None:
            _schedule_on_loop(shutdown)
            return
        _restore_terminal()
        sig_name = signal.Signals(signum).name
        console.print(f"\nReceived {sig_name}, goodbye!")
        raise SystemExit(0)

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
        turn_interrupt_evt = asyncio.Event()
        _turn_done_ref[0] = turn_done
        _turn_interrupt_evt_ref[0] = turn_interrupt_evt
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

            user_intr = turn_interrupt_evt.is_set()
            await interrupt_cli_agent_turn(
                turn_done=turn_done,
                turn_response=turn_response,
                turn_skills=turn_skills,
                dispatch_tasks=tasks,
                cancel_subagents=_cancel_subagents,
                on_interrupted=lambda: console.print("\n\n[dim]Interrupted[/dim]"),
                user_interrupted=user_intr,
            )
            _exit_armed_until[0] = 0.0
            turn_interrupt_evt.clear()

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
        _install_loop_sigint()

        async def _handle_prompt_keyboard_interrupt() -> None:
            evt = _turn_done_ref[0]
            now = time.monotonic()
            action = resolve_prompt_ctrl_c_action(
                turn_done_set=evt.is_set() if evt is not None else True,
                exit_armed_until=_exit_armed_until[0],
                now=now,
            )
            if action == PROMPT_CTRL_C_EXIT:
                _exit_armed_until[0] = 0.0
                _restore_terminal()
                console.print("\nGoodbye!")
                raise _CliSessionExit()
            _exit_armed_until[0] = now + CLI_DOUBLE_CTRL_C_WINDOW_SEC
            console.print(f"\n[dim]{CLI_CTRL_C_EXIT_HINT}[/dim]")

        try:
            while True:
                try:
                    _flush_pending_tty_input()
                    _uninstall_loop_sigint()
                    try:
                        try:
                            user_input = await _read_interactive_input_async()
                        except KeyboardInterrupt:
                            await _handle_prompt_keyboard_interrupt()
                            continue
                    finally:
                        _install_loop_sigint()

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
                    _exit_armed_until[0] = 0.0
                    turn_interrupt_evt.clear()

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

                    turn_interrupted = False
                    try:
                        with _thinking_ctx():
                            try:
                                turn_interrupted = await wait_cli_turn_or_interrupt(
                                    turn_done=turn_done,
                                    interrupt_evt=turn_interrupt_evt,
                                )
                            except KeyboardInterrupt:
                                turn_interrupted = True
                                if not turn_interrupt_evt.is_set():
                                    turn_interrupt_evt.set()
                    finally:
                        if bus_task.done():
                            bus_exc = bus_task.exception()
                            if bus_exc is not None:
                                console.print(
                                    "\n[red]Agent loop stopped unexpectedly. "
                                    "Check network/API settings or run `mira research --logs`.[/red]"
                                )
                                logger.exception("Agent loop task failed")
                        if (
                            not turn_done.is_set()
                            and not turn_interrupted
                            and not turn_interrupt_evt.is_set()
                        ):
                            console.print(
                                "\n[yellow]No response received (request may have timed out).[/yellow]"
                            )
                            turn_done.set()

                    if turn_interrupted:
                        await _cancel_current_turn()
                        continue

                    if turn_response:
                        _print_agent_response(turn_response[0], render_markdown=markdown)
                    elif turn_done.is_set():
                        console.print(
                            "\n[dim]No assistant reply was produced for this turn.[/dim]"
                        )
                    if verbose_mode:
                        used = ", ".join(sorted(turn_skills)) if turn_skills else "none"
                        console.print(f"  [cyan]↳ skills used:[/cyan] {used}")
                except KeyboardInterrupt:
                    evt = _turn_done_ref[0]
                    if evt is not None and not evt.is_set():
                        if not turn_interrupt_evt.is_set():
                            turn_interrupt_evt.set()
                        await _cancel_current_turn()
                        continue
                    await _handle_prompt_keyboard_interrupt()
        except KeyboardInterrupt:
            evt = _turn_done_ref[0]
            if evt is not None and not evt.is_set():
                if _turn_interrupt_evt_ref[0] is not None:
                    _turn_interrupt_evt_ref[0].set()
                await _cancel_current_turn()
        except _CliSessionExit:
            pass
        except EOFError:
            _restore_terminal()
            console.print("\nGoodbye!")
        except Exception:
            logger.exception("Interactive CLI session failed")
            _restore_terminal()
            console.print(
                "\n[red]Session ended due to an unexpected error. "
                "Run with --logs for details.[/red]"
            )
        finally:
            _uninstall_loop_sigint()
            outbound_task.cancel()
            bus_task.cancel()
            try:
                await asyncio.gather(bus_task, outbound_task, return_exceptions=True)
            except KeyboardInterrupt:
                pass

    try:
        asyncio.run(run_interactive())
    except KeyboardInterrupt:
        # Last-resort: avoid traceback if SIGINT escapes after session teardown.
        _restore_terminal()


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


def _resolve_cli_session_id(session_id: str | None, *, prefix: str) -> str:
    """Resolve the session id for an interactive CLI invocation.

    Each CLI process must default to its *own* isolated session so that two
    concurrently running ``mira`` instances never append to the same session
    transcript (which would interleave/cross-contaminate their conversations).

    - When the user passes ``--session``/``-s`` explicitly, honour it verbatim
      so an existing conversation can be resumed.
    - Otherwise mint a unique, per-process session id (``<prefix>:<uuid>``),
      mirroring how Cursor/Codex/Claude Code give every instance a fresh
      session while still sharing global config/persona/long-term memory.
    """
    if session_id is not None and session_id.strip():
        return session_id.strip()
    return f"{prefix}:{uuid.uuid4().hex[:12]}"


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str | None = typer.Option(
        None,
        "--session",
        "-s",
        help="Session ID to resume. Defaults to a unique per-instance session so "
        "concurrent CLIs stay isolated.",
    ),
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

    session_id = _resolve_cli_session_id(session_id, prefix="cli")

    if workspace is None and sys.stdin.isatty():
        if typer.confirm("Do you want to use the current directory as a project workspace?"):
            workspace = os.getcwd()

    config = _load_runtime_config(config, workspace)

    sync_workspace_templates(config.workspace_path)

    # First-run profile setup (interactive terminals only; no-op once onboarded).
    from mira_engine.config.loader import get_config_path

    _run_profile_prompts(config, get_config_path())

    bus = MessageBus()
    provider = _make_provider(config)

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
        workspace=config.workspace_path,
    )


@app.command()
def research(
    message: str = typer.Option(None, "--message", help="Message to send to the research agent"),
    session_id: str | None = typer.Option(
        None,
        "--session",
        "-s",
        help="Session ID to resume. Defaults to a unique per-instance session so "
        "concurrent CLIs stay isolated.",
    ),
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
        help="Agent profile: default | engineer | research | team. Selects AGENTS_*.md bootstrap.",
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

    session_id = _resolve_cli_session_id(session_id, prefix="research")

    mode_value = (mode or "manual").strip().lower()
    if mode_value not in {"manual", "auto"}:
        console.print(
            f"[red]Invalid --mode value: {mode!r}. Expected one of: manual, auto.[/red]"
        )
        raise typer.Exit(1)
    profile_value = (profile or "default").strip().lower()
    if profile_value not in {"default", "engineer", "research", "team"}:
        console.print(
            f"[red]Invalid --profile value: {profile!r}. "
            "Expected one of: default, engineer, research, team.[/red]"
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

    # First-run profile setup (interactive terminals only; no-op once onboarded).
    from mira_engine.config.loader import get_config_path

    _run_profile_prompts(config, get_config_path())

    bus = MessageBus()
    provider = _make_provider(config)

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
        ),
        auto_max_rounds=config.agents.defaults.auto_max_rounds,
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
        workspace=config.workspace_path,
    )


# ============================================================================
# Model Cache Commands
# ============================================================================


models_app = typer.Typer(help="Fetch and manage provider model lists")
app.add_typer(models_app, name="models")


@models_app.command("fetch")
def models_fetch(
    provider: str | None = typer.Argument(None, help="Provider name (defaults to configured provider)"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    select_model: bool = typer.Option(False, "--select", help="Select a model after fetching"),
):
    """Fetch provider models and cache them locally."""
    from mira_engine.config.loader import save_config
    from mira_engine.providers.model_fetch import fetch_models_to_cache

    cfg, config_path = _load_models_command_config(config)
    provider_name = _resolve_models_provider(cfg, provider)
    try:
        cache, cache_path = asyncio.run(fetch_models_to_cache(cfg, provider_name, config_path))
    except Exception as e:
        console.print(f"[red]Model fetch failed:[/red] {e}")
        raise typer.Exit(1)

    console.print(
        f"[green]✓[/green] Fetched {len(cache.models)} models for {cache.provider} "
        f"-> {cache_path}"
    )

    if select_model and cache.models:
        selected = _prompt_cached_model(cache)
        if selected is None:
            return
        cfg.agents.defaults.provider = cache.provider
        cfg.agents.defaults.model = selected.config_model
        save_config(cfg, config_path)
        console.print(f"[green]✓[/green] Set default model to {selected.config_model}")


@models_app.command("list")
def models_list(
    provider: str | None = typer.Argument(None, help="Provider name (omit to list all caches)"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    refresh: bool = typer.Option(False, "--refresh", help="Fetch before listing"),
):
    """List cached provider models."""
    from mira_engine.providers.model_fetch import fetch_models_to_cache, read_all_model_caches, read_model_cache

    cfg, config_path = _load_models_command_config(config)
    caches = []
    if refresh:
        provider_name = _resolve_models_provider(cfg, provider)
        try:
            cache, _ = asyncio.run(fetch_models_to_cache(cfg, provider_name, config_path))
        except Exception as e:
            console.print(f"[red]Model fetch failed:[/red] {e}")
            raise typer.Exit(1)
        caches = [cache]
    elif provider:
        cache = read_model_cache(provider, config_path)
        caches = [cache] if cache else []
    else:
        caches = read_all_model_caches(config_path)

    if not caches:
        console.print("[yellow]No cached models found. Run `mira models fetch` first.[/yellow]")
        return

    console.print(_model_cache_table(caches))
    for cache in caches:
        table = Table(title=f"{cache.provider} models")
        table.add_column("Model")
        table.add_column("Config Value")
        table.add_column("Context", justify="right")
        for model in cache.models:
            context = str(model.context_window_tokens) if model.context_window_tokens else "-"
            table.add_row(model.display_name or model.id, model.config_model, context)
        console.print(table)


@models_app.command("select")
def models_select(
    provider: str | None = typer.Argument(None, help="Provider name (defaults to configured provider)"),
    model: str | None = typer.Option(None, "--model", "-m", help="Model id or config model to select"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    refresh: bool = typer.Option(False, "--refresh", help="Fetch before selecting"),
):
    """Select a cached model and write it to config.json."""
    from mira_engine.config.loader import save_config
    from mira_engine.providers.model_fetch import fetch_models_to_cache, read_model_cache

    cfg, config_path = _load_models_command_config(config)
    provider_name = _resolve_models_provider(cfg, provider)
    if refresh:
        try:
            cache, _ = asyncio.run(fetch_models_to_cache(cfg, provider_name, config_path))
        except Exception as e:
            console.print(f"[red]Model fetch failed:[/red] {e}")
            raise typer.Exit(1)
    else:
        cache = read_model_cache(provider_name, config_path)
        if cache is None:
            console.print("[yellow]No cached models found. Run `mira models fetch` first.[/yellow]")
            raise typer.Exit(1)

    selected = _find_cached_model(cache, model) if model else _prompt_cached_model(cache)
    if selected is None:
        console.print("[red]Model not found in cache.[/red]" if model else "[yellow]No model selected.[/yellow]")
        raise typer.Exit(1)

    cfg.agents.defaults.provider = cache.provider
    cfg.agents.defaults.model = selected.config_model
    save_config(cfg, config_path)
    console.print(f"[green]✓[/green] Set default model to {selected.config_model}")


@models_app.command("clear")
def models_clear(
    provider: str | None = typer.Argument(None, help="Provider name (omit to clear all model caches)"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
):
    """Clear cached model lists."""
    from mira_engine.providers.model_fetch import clear_model_cache

    _cfg, config_path = _load_models_command_config(config)
    removed = clear_model_cache(provider, config_path)
    if not removed:
        console.print("[yellow]No model cache files removed.[/yellow]")
        return
    console.print(f"[green]✓[/green] Removed {len(removed)} model cache file(s).")


def _prompt_cached_model(cache):
    """Prompt for a model from a cache in interactive terminals."""
    if not cache.models:
        return None
    if not sys.stdin.isatty():
        return None

    console.print(f"\nModels for {cache.provider}:")
    for idx, item in enumerate(cache.models, 1):
        console.print(f"  {idx}. {item.config_model}")
    raw = typer.prompt("Select model number", default="", show_default=False).strip()
    if not raw:
        return None
    if not raw.isdigit():
        return None
    idx = int(raw)
    if not 1 <= idx <= len(cache.models):
        return None
    return cache.models[idx - 1]


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
    names.update(type(cfg.channels).builtin_channel_names())

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
# Community Commands
# ============================================================================

community_app = typer.Typer(help="Mira Community Platform (mira-intelligence)")
app.add_typer(community_app, name="community")


def _load_community_config(config: str | None):
    """Load config (optionally from a custom path) for community commands."""
    from mira_engine.config.loader import load_config, set_config_path

    if config:
        set_config_path(Path(config).expanduser().resolve())
    return load_config()


@community_app.command("login")
def community_login(
    api_base: str = typer.Option(None, "--api-base", help="Community API base URL"),
    autonomy: str = typer.Option(
        None, "--autonomy", help="Autonomy mode: fully_autonomous | hitl | hybrid"
    ),
    code_host: str = typer.Option(None, "--code-host", help="Preferred code host: github | cnb"),
    domains: str = typer.Option(
        None, "--domains", help="Comma-separated areas of interest (e.g. channels,agent)"
    ),
    tech_stack: str = typer.Option(None, "--tech-stack", help="Public profile: comma-separated tech (e.g. python,react)"),
    languages: str = typer.Option(None, "--languages", help="Public profile: languages you work in (e.g. en,zh)"),
    participation: str = typer.Option(
        None, "--participation", help="Public profile: any of answer,review,develop,discuss (default: all)"
    ),
    bio: str = typer.Option(None, "--bio", help="Public profile: one or two sentences about you"),
    name: str = typer.Option(
        None, "--name", help="Your agent's community name (default: the name in SOUL.local.md)"
    ),
    no_input: bool = typer.Option(False, "--no-input", help="Do not prompt; use only the options given"),
    config: str | None = typer.Option(None, "--config", help="Path to config.json"),
):
    """Pair this agent with the Mira Community Platform (device flow)."""
    import time
    import webbrowser

    from mira_engine.community.pairing import PairingError, fetch_me, poll_pairing, start_pairing
    from mira_engine.community.public_profile import (
        PublicProfileError,
        normalize_public_profile,
        pairing_payload,
        suggested_agent_name,
    )
    from mira_engine.config.loader import get_config_path, save_config

    cfg = _load_community_config(config)
    interactive = not no_input and sys.stdin.isatty() and sys.stdout.isatty()
    base = (api_base or cfg.community.api_base).rstrip("/")

    if autonomy:
        if autonomy not in ("fully_autonomous", "hitl", "hybrid"):
            console.print("[red]--autonomy must be fully_autonomous, hitl, or hybrid[/red]")
            raise typer.Exit(1)
        cfg.community.autonomy_mode = autonomy
    if code_host:
        if code_host not in ("github", "cnb"):
            console.print("[red]--code-host must be github or cnb[/red]")
            raise typer.Exit(1)
        cfg.community.code_host = code_host
    raw_profile: dict = {
        "name": name if name is not None else suggested_agent_name(cfg.workspace_path),
        "domains": domains if domains is not None else list(cfg.community.domains),
        "tech_stack": tech_stack,
        "languages": languages,
        "participation": participation,
        "bio": bio,
    }
    if interactive and not any(v is not None for v in (name, domains, tech_stack, languages, participation, bio)):
        raw_profile = _prompt_public_profile(raw_profile)
    try:
        public_profile = normalize_public_profile(raw_profile)
    except PublicProfileError as e:
        console.print(f"[red]Public profile: {e}[/red]")
        raise typer.Exit(1)
    cfg.community.domains = public_profile.get("domains", [])

    console.print(f"{__logo__} Pairing with Mira Community at [cyan]{base}[/cyan]\n")

    start_payload = pairing_payload(
        {"code_host": cfg.community.code_host, "autonomy_mode": cfg.community.autonomy_mode, "domains": []},
        public_profile,
    )
    # The same two requests the desktop app makes (mira_engine.community.pairing).
    async def pair() -> tuple[str, str, dict | None]:
        start = await start_pairing(base, start_payload)
        console.print("To authorize this agent, open:")
        console.print(f"  [link={start.verification_url}]{start.verification_url}[/link]")
        if start.user_code:
            console.print(f"and enter code: [bold cyan]{start.user_code}[/bold cyan]")
        try:
            webbrowser.open(start.verification_url)
        except Exception:
            pass
        console.print("\n[dim]Waiting for authorization...[/dim]")

        deadline = time.monotonic() + start.expires_in
        while time.monotonic() < deadline:
            await asyncio.sleep(start.interval)
            poll = await poll_pairing(base, start.poll_token)
            if poll.status == "expired":
                break
            if poll.status == "authorized":
                # The platform has the final say on the name (it falls back to a default).
                return poll.agent_token, poll.agent_id, await fetch_me(base, poll.agent_token)
        return "", "", None

    try:
        agent_token, agent_id, me = asyncio.run(pair())
    except PairingError as e:
        if e.network:
            console.print(f"[red]Network error contacting community: {e}[/red]")
        elif e.status == 400:
            console.print(f"[red]The community rejected the pairing request: {e.detail or e}[/red]")
        else:
            console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if not agent_token:
        console.print("[red]Pairing timed out. Run `mira community login` to try again.[/red]")
        raise typer.Exit(1)

    cfg.community.enabled = True
    cfg.community.api_base = base
    cfg.community.agent_token = agent_token
    cfg.community.agent_id = agent_id
    cfg.community.agent_name = (
        str(me.get("name") or "") if me is not None else public_profile.get("name", "")
    )
    save_config(cfg)
    console.print(
        "\n[green]✓[/green] Joined the Mira Community"
        + (f"  [dim]agent {agent_id}[/dim]" if agent_id else "")
        + (f"  [dim]as {cfg.community.agent_name}[/dim]" if cfg.community.agent_name else "")
    )
    console.print("[dim]A running gateway connects within a few seconds; no restart needed.[/dim]")

    # The private profile (USER.local.md) shapes what the agent may share once
    # the user approves its community persona. Offer to set it up now, while
    # the user is here, instead of joining with nothing to go on.
    if not cfg.profile.onboarded:
        if interactive and typer.confirm(
            "\nSet up your private profile now? It stays on this machine; "
            "you approve anything derived from it before it reaches the community.",
            default=True,
        ):
            _run_profile_prompts(cfg, get_config_path())
        else:
            console.print("[dim]Tip: run `mira profile` to set up your private profile.[/dim]")


def _prompt_public_profile(defaults: dict) -> dict:
    """Ask for the public profile; Enter keeps the default or skips the field."""
    console.print(
        "[bold]Public profile[/bold] [dim](visible to other members; press Enter to skip a field)[/dim]"
    )

    def ask(label: str, default: object) -> str:
        shown = ", ".join(default) if isinstance(default, list) else (default or "")
        return typer.prompt(f"  {label}", default=shown, show_default=bool(shown)).strip()

    return {
        "name": ask("Your agent's community name (no spaces)", defaults.get("name")),
        "domains": ask("Areas you care about (comma-separated)", defaults.get("domains")),
        "tech_stack": ask("Tech stack (e.g. python, react)", defaults.get("tech_stack")),
        "languages": ask("Languages you work in (e.g. en, zh)", defaults.get("languages")),
        "participation": ask(
            "Take part in: answer, review, develop, discuss (blank = all)", defaults.get("participation")
        ),
        "bio": ask("One line about you", defaults.get("bio")),
    }


@community_app.command("profile")
def community_profile(
    name: str = typer.Option(None, "--name", help="New community name (no spaces)"),
    bio: str = typer.Option(None, "--bio", help="New bio ('' clears it)"),
    domains: str = typer.Option(None, "--domains", help="Comma-separated areas ('' clears them)"),
    tech_stack: str = typer.Option(None, "--tech-stack", help="Comma-separated tech ('' clears it)"),
    languages: str = typer.Option(None, "--languages", help="Comma-separated languages ('' clears them)"),
    participation: str = typer.Option(
        None, "--participation", help="Any of answer,review,develop,discuss"
    ),
    config: str | None = typer.Option(None, "--config", help="Path to config.json"),
):
    """Show your agent's community name and public profile, or change them."""
    import httpx

    from mira_engine.community.public_profile import PublicProfileError, normalize_profile_patch
    from mira_engine.config.loader import save_config

    cfg = _load_community_config(config)
    c = cfg.community
    if not c.agent_token:
        console.print("[red]Not logged in. Run `mira community login` first.[/red]")
        raise typer.Exit(1)
    base = c.api_base.rstrip("/")
    headers = {"Authorization": f"Bearer {c.agent_token}"}
    raw = {
        key: value
        for key, value in (
            ("name", name), ("bio", bio), ("domains", domains), ("tech_stack", tech_stack),
            ("languages", languages), ("participation", participation),
        )
        if value is not None
    }
    try:
        fields = normalize_profile_patch(raw)
    except PublicProfileError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    try:
        with httpx.Client(timeout=15.0) as client:
            if fields:
                resp = client.patch(f"{base}/agents/me/profile", json=fields, headers=headers)
                if resp.status_code >= 400:
                    detail = resp.json().get("error") if resp.headers.get("content-type", "").startswith("application/json") else resp.text
                    console.print(f"[red]The community rejected the change: {detail}[/red]")
                    raise typer.Exit(1)
            me = client.get(f"{base}/agents/me", headers=headers)
            me.raise_for_status()
            data = me.json()
    except httpx.HTTPError as e:
        console.print(f"[red]Network error contacting community: {e}[/red]")
        raise typer.Exit(1)
    profile = data.get("profile") or {}
    c.agent_name = str(data.get("name") or c.agent_name or "")
    if isinstance(profile.get("domains"), list):
        c.domains = list(profile["domains"])
    if fields:
        save_config(cfg)
        console.print("[green]✓[/green] Profile saved\n")
    agent_id = str(data.get("id") or c.agent_id or "")
    console.print(f"Name: [bold]{c.agent_name or '-'}[/bold]" + (f" [dim]#{agent_id[:8]}[/dim]" if agent_id else ""))
    for label, key in (("Areas", "domains"), ("Tech stack", "tech_stack"), ("Languages", "languages"), ("Takes part in", "participation")):
        values = profile.get(key) or []
        console.print(f"{label}: {', '.join(values) if values else '[dim]none[/dim]'}")
    console.print(f"Bio: {profile.get('bio') or '[dim]none[/dim]'}")


@community_app.command("notifications")
def community_notifications(
    kinds: str = typer.Argument(
        None, help="Comma-separated kinds to receive (reply,mention,review,work,activity), or 'none'"
    ),
    config: str | None = typer.Option(None, "--config", help="Path to config.json"),
):
    """Show or choose which community events reach your inbox."""
    from mira_engine.community.inbox import NOTIFICATION_KINDS, normalize_kinds
    from mira_engine.community.public_profile import split_list
    from mira_engine.config.loader import save_config

    cfg = _load_community_config(config)
    if kinds is not None:
        chosen = [] if kinds.strip().lower() == "none" else split_list(kinds)
        try:
            cfg.community.notifications = normalize_kinds(chosen)
        except ValueError as e:
            console.print(f"[red]{e}[/red] (choose from {', '.join(NOTIFICATION_KINDS)}, or 'none')")
            raise typer.Exit(1)
        save_config(cfg)
    current = list(cfg.community.notifications or [])
    if current:
        console.print(f"Notifications: {', '.join(current)}")
    else:
        console.print("Notifications: none [dim](the agent runs on its own; approvals still need you)[/dim]")


@community_app.command("approvals")
def community_approvals_cmd(
    action: str = typer.Argument(None, help="approve or reject (omit to list what is pending)"),
    approval_id: str = typer.Argument(None, help="The approval id (from the list)"),
    config: str | None = typer.Option(None, "--config", help="Path to config.json"),
):
    """List or decide community actions held for your approval (#72).

    Decisions go through the running gateway when there is one, so the agent
    follows up in the chat the action came from; without a gateway the action
    runs here directly.
    """
    import asyncio

    import httpx

    from mira_engine.community import approvals

    cfg = _load_community_config(config)
    if action is None:
        pending = approvals.list_approvals(status="pending")
        if not pending:
            console.print("No community actions are waiting for your approval.")
            return
        console.print(f"{__logo__} {len(pending)} community action(s) waiting for your approval\n")
        for rec in pending:
            console.print(f"  [cyan]{rec['id']}[/cyan]  {approvals.summarize(rec)}  [dim]{rec.get('created_at', '')}[/dim]")
        console.print("\nDecide with: mira community approvals approve|reject <id>")
        return
    if action not in ("approve", "reject") or not approval_id:
        console.print("[red]Usage: mira community approvals [approve|reject <id>][/red]")
        raise typer.Exit(2)
    record = approvals.get_approval(approval_id)
    if record is None:
        console.print(f"[red]No community approval {approval_id}.[/red]")
        raise typer.Exit(1)
    if record.get("status") != "pending":
        console.print(f"Approval {approval_id} is already {record.get('status')}.")
        raise typer.Exit(1)

    # Prefer the gateway: it executes, records and follows up in the chat.
    from mira_engine.config.loader import get_config_path
    from mira_engine.security.gateway_auth import local_gateway_tokens

    host = cfg.gateway.host
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    url = f"http://{host}:{cfg.gateway.port}/api/community/approvals/{approval_id}/decide"
    for token in local_gateway_tokens(get_config_path()) or [""]:
        try:
            with httpx.Client(timeout=120.0, trust_env=False) as client:
                resp = client.post(url, json={"decision": action}, headers={"Authorization": f"Bearer {token}"} if token else {})
        except httpx.HTTPError:
            break  # no gateway: decide locally below
        if resp.status_code == 401:
            continue
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = {}
        if resp.status_code < 400 and data.get("ok"):
            console.print(f"[green]✓[/green] {action}d: {data.get('result') or approvals.summarize(record)}")
            return
        console.print(f"[red]{action} failed: {data.get('error') or resp.text[:200]}[/red]")
        raise typer.Exit(1)

    if action == "reject":
        approvals.set_status(approval_id, "rejected")
        console.print(f"[green]✓[/green] rejected: {approvals.summarize(record)}")
        return
    result = asyncio.run(approvals.execute_approval(record, cfg.community))
    approvals.set_status(approval_id, "approved" if result.get("ok") else "failed", detail=str(result.get("detail") or ""))
    if result.get("ok"):
        console.print(f"[green]✓[/green] approved and done: {result.get('detail')}")
    else:
        console.print(f"[red]approved, but it failed: {result.get('detail')}[/red]")
        raise typer.Exit(1)


@community_app.command("status")
def community_status(
    config: str | None = typer.Option(None, "--config", help="Path to config.json"),
):
    """Show Mira Community connection status."""
    import httpx

    cfg = _load_community_config(config)
    c = cfg.community

    console.print(f"{__logo__} Mira Community\n")
    console.print(f"Enabled: {'[green]✓[/green]' if c.enabled else '[dim]no[/dim]'}")
    console.print(f"API base: {c.api_base}")
    console.print(
        "Logged in: "
        + ("[green]✓[/green]" if c.agent_token else "[dim]no — run `mira community login`[/dim]")
        + (f"  [dim]agent {c.agent_id}[/dim]" if c.agent_id else "")
    )
    console.print(f"Autonomy: {c.autonomy_mode}")
    console.print(f"Code host: {c.code_host}")
    console.print(f"Domains: {', '.join(c.domains) if c.domains else '[dim]none[/dim]'}")

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(f"{c.api_base.rstrip('/')}/health")
        if resp.status_code == 200:
            console.print(f"Service: [green]✓ reachable[/green] [dim]{resp.text.strip()}[/dim]")
        else:
            console.print(f"Service: [yellow]HTTP {resp.status_code}[/yellow]")
    except Exception as e:
        console.print(f"Service: [red]unreachable[/red] [dim]{e}[/dim]")


@community_app.command("rules")
def community_rules(
    config: str | None = typer.Option(None, "--config", help="Path to config.json"),
):
    """Show the current community rules (read-only).

    There is nothing to sign (#33): your agent accepts the rules by posting its
    welcome reply, and keeps them current automatically (delivered on onboarding,
    re-synced on version bumps). This command just shows them for inspection.
    """
    import httpx

    cfg = _load_community_config(config)
    c = cfg.community
    base = c.api_base.rstrip("/")

    try:
        with httpx.Client(timeout=15.0) as client:
            rules = client.get(f"{base}/rules").json()
    except Exception as e:
        console.print(f"[red]Failed to fetch rules: {e}[/red]")
        raise typer.Exit(1)

    version = rules.get("version")
    console.print(f"{__logo__} Mira Community Rules v{version}\n")
    for r in rules.get("rules", []):
        enforced = r.get("enforcement") == "hard"
        tag = "[red](enforced)[/red]" if enforced else "[dim](guideline)[/dim]"
        console.print(f"[bold]{r.get('title')}[/bold] {tag}")
        console.print(f"  {r.get('text')}\n")

    console.print(
        "[dim]Your agent accepts these by posting its welcome reply and keeps "
        "them in sync automatically — nothing to sign.[/dim]"
    )


@community_app.command("persona")
def community_persona(
    approve: bool = typer.Option(False, "--approve", help="Approve the proposed persona"),
    config: str | None = typer.Option(None, "--config", help="Path to config.json"),
):
    """Show (and optionally approve) the community persona drafted from your profile.

    Community turns never see USER.md; they see COMMUNITY_PERSONA.md, which is
    derived from your profile minus anything identifying and only changes when
    you approve it (mira#212).
    """
    from mira_engine.community.persona import apply_persona, draft_persona, persona_diff, persona_path
    from mira_engine.community.privacy import default_guard
    from mira_engine.config.loader import load_config, set_config_path

    if config:
        set_config_path(Path(config).expanduser().resolve())
    cfg = load_config()
    workspace = cfg.workspace_path
    texts = [p.read_text(encoding="utf-8") for p in (workspace / "USER.md", workspace / "USER.local.md") if p.exists()]
    # Same inputs as the channel's automatic draft: the public profile when the
    # platform is reachable, else at least the configured domains.
    profile: dict = {"domains": list(cfg.community.domains or [])}
    if cfg.community.api_base and cfg.community.agent_token:
        try:
            import httpx

            resp = httpx.get(
                f"{cfg.community.api_base.rstrip('/')}/agents/me",
                headers={"Authorization": f"Bearer {cfg.community.agent_token}"},
                timeout=10.0,
            )
            if resp.status_code == 200 and isinstance((resp.json() or {}).get("profile"), dict):
                profile = resp.json()["profile"]
        except Exception:  # noqa: BLE001 - offline: fall back to the config
            pass
    draft = draft_persona(texts, profile, default_guard())
    current = persona_path(workspace).read_text(encoding="utf-8") if persona_path(workspace).exists() else ""
    if draft == current:
        console.print("The community persona is up to date.")
        return
    console.print(persona_diff(current, draft) or draft)
    if approve:
        console.print(f"[green]✓[/green] Written to {apply_persona(workspace, draft)}")
    else:
        console.print("Run with --approve to use this persona in community turns.")


@community_app.command("report")
def community_report(
    config: str | None = typer.Option(None, "--config", help="Path to config.json"),
):
    """Write this week's community harvest report (local only) and print it."""
    from mira_engine.community.budget import CommunityBudget
    from mira_engine.community.client import CommunityClient
    from mira_engine.community.harvest import write_report
    from mira_engine.config.loader import load_config, set_config_path

    if config:
        set_config_path(Path(config).expanduser().resolve())
    cfg = load_config()
    cc = cfg.community
    if not (cc.api_base and cc.agent_token):
        console.print("[red]Not connected to the community (run `mira community login`).[/red]")
        raise typer.Exit(1)
    budget = CommunityBudget(int(cc.daily_token_budget or 0))
    path = asyncio.run(write_report(CommunityClient(cc.api_base, cc.agent_token), cfg.workspace_path, cc, budget))
    console.print(Markdown(path.read_text(encoding="utf-8")))
    console.print(f"[dim]Saved to {path}[/dim]")


@community_app.command("onboard")
def community_onboard(
    config: str | None = typer.Option(None, "--config", help="Path to config.json"),
):
    """Drive the agent to complete the onboarding connection test.

    Nudges the running gateway's agent to compose and post its own reply in the
    welcome thread, which verifies this agent and records rules acceptance (#33).
    Use this if the agent did not auto-onboard. Safe to re-run: it is skipped
    when you are already onboarded or a reply is already in flight.

    Requires the gateway to be running (`mira gateway`).
    """
    import httpx

    cfg = _load_community_config(config)
    c = cfg.community
    if not c.agent_token:
        console.print("[red]Not logged in — run `mira community login` first.[/red]")
        raise typer.Exit(1)

    host = cfg.gateway.host
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    url = f"http://{host}:{cfg.gateway.port}/api/community/onboard"

    console.print(f"{__logo__} Asking the agent to complete onboarding...")
    try:
        # trust_env=False: never route the loopback call through a system/env
        # proxy (macOS system proxies otherwise 502 on 127.0.0.1).
        with httpx.Client(timeout=30.0, trust_env=False) as client:
            resp = client.post(url, json={})
    except httpx.ConnectError:
        console.print(
            "[red]The gateway isn't running.[/red] Start it with "
            "[cyan]mira gateway[/cyan], then re-run this command."
        )
        raise typer.Exit(1)
    except httpx.HTTPError as e:
        console.print(f"[red]Could not reach the gateway: {e}[/red]")
        raise typer.Exit(1)

    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code >= 400 or not data.get("ok"):
        detail = (
            (data.get("error") if isinstance(data, dict) else None)
            or (resp.text or "").strip()[:200]
            or f"HTTP {resp.status_code} (empty response)"
        )
        console.print(f"[red]Onboarding failed: {detail}[/red]")
        raise typer.Exit(1)

    if data.get("already"):
        status = data.get("status") or "active"
        console.print(
            f"[green]✓[/green] Already onboarded [dim](status: {status})[/dim] — "
            "nothing to do."
        )
    elif data.get("pending"):
        console.print(
            "[yellow]⏳[/yellow] A welcome reply is already in flight — give the agent "
            "a moment, then check [cyan]mira community status[/cyan]."
        )
    else:
        console.print(
            "[green]✓[/green] Asked your agent to introduce itself in the welcome thread.\n"
            "[dim]It will reply shortly; check `mira community status` for active status.[/dim]"
        )


@community_app.command("logout")
def community_logout(
    config: str | None = typer.Option(None, "--config", help="Path to config.json"),
):
    """Disconnect this agent from the Mira Community Platform."""
    from mira_engine.config.loader import save_config

    cfg = _load_community_config(config)
    # Only clear credentials — leave ``enabled`` alone so the always-on channel
    # idles instead of being torn down. A running gateway auto-disconnects when
    # it notices the token is gone; no restart needed.
    cfg.community.agent_token = ""
    cfg.community.agent_id = ""
    save_config(cfg)
    console.print("[green]✓[/green] Left the Mira Community (local credentials cleared)")


# ============================================================================
# Status Commands
# ============================================================================


def _oauth_login_status(provider_name: str) -> tuple[bool, str | None]:
    """Return local OAuth login state without doing any network calls
    (the ``login_status`` hook of the plugin that provides the provider)."""
    from mira_engine.plugins import get_runtime

    try:
        hook = get_runtime().provider_hook(provider_name, "login_status")
        if hook is None:
            return False, None
        logged_in, account_id = hook()
    except Exception:
        return False, None
    return (True, str(account_id) if account_id else None) if logged_in else (False, None)


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
        from mira_engine.providers.factory import plugin_provider_unavailable
        from mira_engine.providers.model_fetch import is_cache_stale, read_model_cache
        from mira_engine.providers.registry import all_providers

        console.print(f"Model: {_format_model_selection(config.agents.defaults.model)}")
        active_provider = (
            config.get_provider_name(config.agents.defaults.model) or config.agents.defaults.provider
        ).replace("-", "_")
        for _role in ("supervisor", "student", "critic"):
            _role_model = getattr(config.agents.defaults, f"{_role}_model", None)
            if _role_model:
                console.print(f"Team {_role}: {_format_model_selection(_role_model)}")

        # Check API keys from registry
        for spec in all_providers():
            p = config.providers.entry(spec.name)
            if p is None:
                continue
            if spec.is_oauth:
                authenticated, account_id = _oauth_login_status(spec.name)
                unavailable = plugin_provider_unavailable(spec) if spec.plugin_id else None
                if unavailable:
                    console.print(f"{spec.label}: [yellow]{unavailable}[/yellow]")
                elif authenticated:
                    account_part = f" [dim]{account_id}[/dim]" if account_id else ""
                    console.print(f"{spec.label}: [green]✓ OAuth[/green]{account_part}")
                else:
                    console.print(f"{spec.label}: [dim]not logged in[/dim]")
            elif spec.is_local:
                # Local deployments show api_base instead of api_key
                if p.api_base:
                    console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(p.api_key)
                console.print(f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}")
            if spec.name == active_provider:
                cache = read_model_cache(spec.name, config_path)
                if cache:
                    state = "stale" if is_cache_stale(cache) else "fresh"
                    console.print(
                        f"  Models: [green]{len(cache.models)} cached[/green] "
                        f"({state}, fetched {cache.fetched_at})"
                    )
                    cached_models = {model.config_model for model in cache.models}
                    if config.agents.defaults.model not in cached_models:
                        console.print(
                            "  [yellow]! Current model is not in the cached model list. "
                            "Run `mira models fetch` to refresh.[/yellow]"
                        )


# ============================================================================
# OAuth Login (used by onboarding)
# ============================================================================


def _run_oauth_login(provider_name: str) -> None:
    """Interactive login: the ``login`` hook of the plugin that provides the
    provider (``login(print_fn, prompt_fn)``, raising on failure)."""
    from mira_engine.plugins import get_runtime
    from mira_engine.providers.registry import find_by_name

    spec = find_by_name(provider_name)
    if not spec or not spec.is_oauth:
        raise typer.Exit(1)
    handler = get_runtime().provider_hook(spec.name, "login")
    if not handler:
        from mira_engine.providers.factory import plugin_provider_unavailable

        console.print(f"[red]{plugin_provider_unavailable(spec) or f'OAuth login not implemented for {spec.label}'}[/red]")
        raise typer.Exit(1)
    console.print(f"\n{__logo__} OAuth Login - {spec.label}\n")
    try:
        handler(print_fn=lambda s: console.print(s), prompt_fn=lambda s: typer.prompt(s))
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1) from e
    console.print(f"[green]✓ Authenticated with {spec.label}[/green]")


if __name__ == "__main__":
    app()
