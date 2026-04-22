"""High-level programmatic interface to mira."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mira_engine.agent.hook import AgentHook
from mira_engine.agent.loop import AgentLoop
from mira_engine.bus.queue import MessageBus
from mira_engine.providers.factory import make_provider


@dataclass(slots=True)
class RunResult:
    """Result of a single agent run."""

    content: str
    tools_used: list[str]
    messages: list[dict[str, Any]]


class Mira:
    """Programmatic facade for running the mira agent.

    Usage::

        bot = Mira.from_config()
        result = await bot.run("Summarize this repo", hooks=[MyHook()])
        print(result.content)
    """

    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    @classmethod
    def from_config(
        cls,
        config_path: str | Path | None = None,
        *,
        workspace: str | Path | None = None,
    ) -> Mira:
        """Create a Mira instance from a config file.

        Args:
            config_path: Path to ``config.json``.  Defaults to
                ``~/.mira/config.json``.
            workspace: Override the workspace directory from config.
        """
        from mira_engine.config.loader import load_config, resolve_config_env_vars
        from mira_engine.config.schema import Config

        resolved: Path | None = None
        if config_path is not None:
            resolved = Path(config_path).expanduser().resolve()
            if not resolved.exists():
                raise FileNotFoundError(f"Config not found: {resolved}")

        config: Config = resolve_config_env_vars(load_config(resolved))
        if workspace is not None:
            config.agents.defaults.workspace = str(
                Path(workspace).expanduser().resolve()
            )

        provider = _make_provider(config)
        bus = MessageBus()
        defaults = config.agents.defaults

        loop = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=config.workspace_path,
            model=defaults.model,
            max_iterations=defaults.max_tool_iterations,
            context_window_tokens=defaults.context_window_tokens,
            exec_config=config.tools.exec,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            mcp_servers=config.tools.mcp_servers,
            timezone=defaults.timezone,
            unified_session=defaults.unified_session,
        )
        loop.max_tool_result_chars = defaults.max_tool_result_chars
        loop.provider_retry_mode = defaults.provider_retry_mode
        loop.context_block_limit = defaults.context_block_limit
        loop.web_config = config.tools.web
        loop._extra_hooks = []
        return cls(loop)

    async def run(
        self,
        message: str,
        *,
        session_key: str = "sdk:default",
        hooks: list[AgentHook] | None = None,
    ) -> RunResult:
        """Run the agent once and return the result.

        Args:
            message: The user message to process.
            session_key: Session identifier for conversation isolation.
                Different keys get independent history.
            hooks: Optional lifecycle hooks for this run.
        """
        prev = self._loop._extra_hooks
        if hooks is not None:
            self._loop._extra_hooks = list(hooks)
        try:
            response = await self._loop.process_direct(
                message, session_key=session_key,
            )
        finally:
            self._loop._extra_hooks = prev

        if response is None:
            content = ""
        elif isinstance(response, str):
            content = response
        else:
            content = (getattr(response, "content", "") or "")
        return RunResult(content=content, tools_used=[], messages=[])


def _make_provider(config: Any) -> Any:
    """Create the LLM provider from config (extracted from CLI)."""
    forced = str(getattr(config.agents.defaults, "provider", "") or "").replace("-", "_")
    model = getattr(config.agents.defaults, "model", None)
    if forced == "github_copilot":
        from mira_engine.providers.github_copilot_provider import GitHubCopilotProvider

        return GitHubCopilotProvider(default_model=model or "github-copilot/gpt-4.1")
    if forced == "openai_codex":
        from mira_engine.providers.openai_codex_provider import OpenAICodexProvider

        return OpenAICodexProvider(default_model=model or "openai-codex/gpt-5.1-codex")
    return make_provider(config, model)
