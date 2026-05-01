"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings

from mira_engine.cron.types import CronSchedule


def normalize_model_candidates(value: str | list[str] | None) -> list[str]:
    """Normalize a model or model-candidates value to a de-duplicated list."""
    if value is None:
        return []
    items = [value] if isinstance(value, str) else list(value)
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        candidate = item.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        result.append(candidate)
    return result


def primary_model_candidate(value: str | list[str] | None, fallback: str | None = None) -> str | None:
    """Return the first valid model candidate, with optional fallback."""
    candidates = normalize_model_candidates(value)
    if candidates:
        return candidates[0]
    return fallback


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

class ChannelsConfig(Base):
    """Configuration for chat channels.

    Built-in and plugin channel configs are stored as extra fields (dicts).
    Each channel parses its own config in __init__.
    Per-channel "streaming": true enables streaming output (requires send_delta impl).
    """

    model_config = ConfigDict(extra="allow")

    send_progress: bool = True  # stream agent's text progress to the channel
    send_tool_hints: bool = False  # stream tool-call hints (e.g. read_file("…"))
    send_max_retries: int = Field(default=3, ge=0, le=10)  # Max delivery attempts (initial send included)
    transcription_provider: str = "groq"  # Voice transcription backend: "groq" or "openai"
    _enable_builtin_access: bool = False

    @classmethod
    def builtin_channel_names(cls) -> tuple[str, ...]:
        return (
            "telegram",
            "whatsapp",
            "discord",
            "feishu",
            "mochat",
            "dingtalk",
            "email",
            "slack",
            "qq",
            "matrix",
            "ui",
        )

    def _ensure_builtin(self, name: str) -> Any:
        extras = self.model_extra
        if extras is None:
            extras = {}
            object.__setattr__(self, "__pydantic_extra__", extras)
        current = extras.get(name)
        if current is not None:
            return current
        defaults: dict[str, Any] = {
            "telegram": TelegramConfig(),
            "whatsapp": WhatsAppConfig(),
            "discord": DiscordConfig(),
            "feishu": FeishuConfig(),
            "mochat": MochatConfig(),
            "dingtalk": DingTalkConfig(),
            "email": EmailConfig(),
            "slack": SlackConfig(),
            "qq": QQConfig(),
            "matrix": MatrixConfig(),
            "ui": UiChannelConfig(),
        }
        value = defaults[name]
        extras[name] = value
        return value

    def __getattr__(self, item: str) -> Any:
        if item in self.builtin_channel_names() and object.__getattribute__(self, "_enable_builtin_access"):
            return self._ensure_builtin(item)
        extras = self.model_extra or {}
        if item in extras:
            return extras[item]
        raise AttributeError(item)


class ChannelConfig(Base):
    """Common channel config fields."""

    model_config = ConfigDict(extra="allow")
    enabled: bool = False
    allow_from: list[str] = Field(default_factory=list)


class TelegramConfig(ChannelConfig):
    token: str = ""
    proxy: str | None = None
    reply_to_message: bool = False
    react_emoji: str = "👀"
    group_policy: Literal["open", "mention"] = "mention"
    connection_pool_size: int = 32
    pool_timeout: float = 5.0
    streaming: bool = True
    stream_edit_interval: float = Field(default=0.6, ge=0.1)


class WhatsAppConfig(ChannelConfig):
    bridge_url: str = "ws://localhost:3001"
    bridge_token: str = ""
    group_policy: Literal["open", "mention"] = "open"


class DiscordConfig(ChannelConfig):
    pass


class FeishuConfig(ChannelConfig):
    app_id: str = ""
    app_secret: str = ""
    encrypt_key: str = ""
    verification_token: str = ""
    react_emoji: str = "THUMBSUP"
    group_policy: Literal["open", "mention"] = "mention"
    reply_to_message: bool = False
    streaming: bool = True


class MochatConfig(ChannelConfig):
    pass


class DingTalkConfig(ChannelConfig):
    client_id: str = ""
    client_secret: str = ""


class EmailConfig(ChannelConfig):
    consent_granted: bool = False
    poll_interval_seconds: int = 30
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_use_ssl: bool = True
    imap_mailbox: str = "INBOX"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    from_address: str = ""
    mark_seen: bool = True
    max_body_chars: int = 12000
    subject_prefix: str = "Re: "
    auto_reply_enabled: bool = True
    verify_dkim: bool = False
    verify_spf: bool = False
    allowed_attachment_types: list[str] = Field(default_factory=list)
    max_attachment_size: int = 10 * 1024 * 1024
    max_attachments_per_email: int = 5


class SlackConfig(ChannelConfig):
    bot_token: str = ""
    app_token: str = ""
    mode: str = "socket"
    react_emoji: str = "eyes"
    dm: "SlackDMConfig" = Field(default_factory=lambda: SlackDMConfig())
    group_policy: Literal["open", "mention", "allowlist"] = "mention"
    group_allow_from: list[str] = Field(default_factory=list)
    reply_in_thread: bool = True


class QQConfig(ChannelConfig):
    app_id: str = ""
    secret: str = ""
    ack_message: str = "⏳ Processing..."
    msg_format: Literal["text", "markdown", "plain"] = "text"


class SlackDMConfig(Base):
    enabled: bool = True
    policy: Literal["open", "allowlist"] = "open"
    allow_from: list[str] = Field(default_factory=list)


class MatrixConfig(ChannelConfig):
    homeserver: str = "https://matrix.org"
    user_id: str = ""
    password: str = ""
    access_token: str = ""
    device_id: str = ""
    e2ee_enabled: bool = Field(default=True, alias="e2eeEnabled")
    sync_stop_grace_seconds: int = 2
    max_media_bytes: int = 20 * 1024 * 1024
    group_policy: Literal["open", "mention", "allowlist"] = "open"
    group_allow_from: list[str] = Field(default_factory=list)
    allow_room_mentions: bool = False
    streaming: bool = False


class UiChannelConfig(Base):
    """UI channel runtime configuration (WebSocket + HTTP for desktop/browser clients)."""

    enabled: bool = False
    allow_from: list[str] = Field(default_factory=list)
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])


# Legacy alias kept for downstream imports that reference the previous name.
# New code should use ``UiChannelConfig`` directly.
WebChannelConfig = UiChannelConfig


class DreamConfig(Base):
    """Dream memory consolidation configuration."""

    _HOUR_MS = 3_600_000

    interval_h: int = Field(default=2, ge=1)  # Every 2 hours by default
    cron: str | None = Field(default=None, exclude=True)  # Legacy compatibility override
    model_override: str | None = Field(
        default=None,
        validation_alias=AliasChoices("modelOverride", "model", "model_override"),
    )  # Optional Dream-specific model override
    max_batch_size: int = Field(default=20, ge=1)  # Max history entries per run
    max_iterations: int = Field(default=10, ge=1)  # Max tool calls per Phase 2

    def build_schedule(self, timezone: str) -> CronSchedule:
        """Build the runtime schedule, preferring the legacy cron override if present."""
        if self.cron:
            return CronSchedule(kind="cron", expr=self.cron, tz=timezone)
        return CronSchedule(kind="every", every_ms=self.interval_h * self._HOUR_MS)

    def describe_schedule(self) -> str:
        """Return a human-readable summary for logs and startup output."""
        if self.cron:
            return f"cron {self.cron} (legacy)"
        hours = self.interval_h
        return f"every {hours}h"


class AgentDefaults(Base):
    """Default agent configuration."""

    workspace: str = "~/.mira/workspace"
    model: str = "anthropic/claude-opus-4-5"
    model_candidates: list[str] = Field(default_factory=list, exclude=True)
    route_model: str | None = None
    route_model_candidates: list[str] = Field(default_factory=list, exclude=True)
    small_model: str | None = None
    small_model_candidates: list[str] = Field(default_factory=list, exclude=True)
    medium_model: str | None = None
    medium_model_candidates: list[str] = Field(default_factory=list, exclude=True)
    large_model: str | None = None
    large_model_candidates: list[str] = Field(default_factory=list, exclude=True)
    route_by_complexity: bool = False
    provider: str = (
        "auto"  # Provider name (e.g. "anthropic", "openrouter") or "auto" for auto-detection
    )
    max_tokens: int = 8192
    context_window_tokens: int = 65_536
    context_block_limit: int | None = None
    temperature: float = 0.1
    max_tool_iterations: int = 200
    max_tool_result_chars: int = 16_000
    provider_retry_mode: Literal["standard", "persistent"] = "standard"
    reasoning_effort: str | None = None  # low / medium / high / adaptive - enables LLM thinking mode
    timezone: str = "UTC"  # IANA timezone, e.g. "Asia/Shanghai", "America/New_York"
    unified_session: bool = False  # Share one session across all channels (single-user multi-device)
    dream: DreamConfig = Field(default_factory=DreamConfig)

    @model_validator(mode="before")
    @classmethod
    def _normalize_candidates_input(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        aliases = {
            "model": ("model",),
            "route_model": ("route_model", "routeModel"),
            "small_model": ("small_model", "smallModel"),
            "medium_model": ("medium_model", "mediumModel"),
            "large_model": ("large_model", "largeModel"),
        }
        for key, key_aliases in aliases.items():
            found = False
            value = None
            matched_alias = None
            for alias in key_aliases:
                if alias in payload:
                    value = payload.get(alias)
                    found = True
                    matched_alias = alias
                    break
            if found:
                candidates = normalize_model_candidates(value)

                # Prepend provider prefix if missing and provider is specified
                provider = payload.get("provider", "auto")
                if provider != "auto":
                    from mira_engine.providers.registry import find_by_name

                    spec = find_by_name(provider)
                    if spec and spec.litellm_prefix:
                        prefix = f"{spec.litellm_prefix}/"
                        candidates = [
                            f"{prefix}{c}" if "/" not in c else c for c in candidates
                        ]

                payload[f"{key}_candidates"] = candidates
                primary = candidates[0] if candidates else None
                payload[key] = primary
                if matched_alias:
                    payload[matched_alias] = primary
        if payload.get("model") is None:
            payload["model"] = "anthropic/claude-opus-4-5"
            payload["model_candidates"] = [payload["model"]]
        return payload

    @property
    def primary_model(self) -> str:
        return self.model

    @property
    def default_model_candidates(self) -> list[str]:
        return self.model_candidates or [self.model]

    @property
    def primary_routing_model(self) -> str:
        return self.route_model or self.small_model or self.primary_model

    @property
    def routing_model_candidates(self) -> list[str]:
        if self.route_model_candidates:
            return self.route_model_candidates
        if self.route_model:
            return [self.route_model]
        return self.tier_model_candidates("small")

    def primary_model_for_tier(self, tier: str) -> str:
        if tier == "small":
            return self.small_model or self.primary_model
        if tier == "medium":
            return self.medium_model or self.primary_model
        if tier == "large":
            return self.large_model or self.primary_model
        return self.primary_model

    def tier_model_candidates(self, tier: str) -> list[str]:
        if tier == "small":
            return self.small_model_candidates or ([self.small_model] if self.small_model else [self.primary_model])
        if tier == "medium":
            return self.medium_model_candidates or ([self.medium_model] if self.medium_model else [self.primary_model])
        if tier == "large":
            return self.large_model_candidates or ([self.large_model] if self.large_model else [self.primary_model])
        return self.default_model_candidates


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class ProviderConfig(Base):
    """LLM provider configuration."""

    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)


class ProvidersConfig(Base):
    """Configuration for LLM providers."""

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # Any OpenAI-compatible endpoint
    azure_openai: ProviderConfig = Field(default_factory=ProviderConfig)  # Azure OpenAI (model = deployment name)
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    ollama: ProviderConfig = Field(default_factory=ProviderConfig)  # Ollama local models
    ovms: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenVINO Model Server (OVMS)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    mistral: ProviderConfig = Field(default_factory=ProviderConfig)
    stepfun: ProviderConfig = Field(default_factory=ProviderConfig)  # Step Fun (阶跃星辰)
    xiaomi_mimo: ProviderConfig = Field(default_factory=ProviderConfig)  # Xiaomi MIMO (小米)
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig)  # SiliconFlow (硅基流动)
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine (火山引擎)
    volcengine_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine Coding Plan
    byteplus: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus (VolcEngine international)
    byteplus_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus Coding Plan
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # OpenAI Codex (OAuth)
    github_copilot: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # Github Copilot (OAuth)
    qianfan: ProviderConfig = Field(default_factory=ProviderConfig)  # Qianfan (百度千帆)


class HeartbeatConfig(Base):
    """Heartbeat service configuration."""

    enabled: bool = True
    interval_s: int = 30 * 60  # 30 minutes
    keep_recent_messages: int = 8


class ApiConfig(Base):
    """OpenAI-compatible API server configuration."""

    host: str = "127.0.0.1"  # Safer default: local-only bind.
    port: int = 8900
    timeout: float = 120.0  # Per-request timeout in seconds.


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "0.0.0.0"
    port: int = 18790
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)


class WebSearchConfig(Base):
    """Web search tool configuration."""

    provider: str = "duckduckgo"  # brave, tavily, duckduckgo, searxng, jina
    api_key: str = ""
    base_url: str = ""  # SearXNG base URL
    max_results: int = 5
    timeout: int = 30  # Wall-clock timeout (seconds) for search operations


class WebToolsConfig(Base):
    """Web tools configuration."""

    enable: bool = True
    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class PythonRuntimeConfig(Base):
    """Per-project Python runtime configuration for the exec tool.

    When ``manager == "uv"`` and ``auto_bootstrap`` is true, the exec tool
    creates a project-local ``.venv`` (configurable via ``venv_dir``) on the
    first python-related command and prepends it to PATH for every subsequent
    subprocess. With ``manager == "off"`` (the default) the exec tool keeps
    its legacy behaviour and resolves ``python`` against the parent process
    environment, leaving environment management entirely to the user.

    See the milestone ``Per-project Python environments`` for design context.
    """

    # ``off`` keeps the historical behaviour. ``uv`` enables per-project venv
    # auto-bootstrap. ``system`` is reserved for a future passthrough mode
    # (no venv, but with explicit interpreter pinning).
    manager: Literal["off", "uv", "system"] = "off"

    # Whether to lazily create the project venv the first time the agent runs
    # a python-shaped command (python, pip, pytest, jupyter, ipython, uv).
    auto_bootstrap: bool = True

    # Project-relative directory for the venv. Resolved against the project
    # working directory at exec time, not against the global workspace.
    venv_dir: str = ".venv"

    # Override for ``$UV_CACHE_DIR``. Empty means "let uv choose its default
    # (``~/.cache/uv`` on Unix, ``%LOCALAPPDATA%\\uv\\cache`` on Windows)".
    cache_dir: str = ""

    # uv link mode for hardlinking wheels from the cache into the venv.
    # ``hardlink`` is the most disk-efficient and is uv's default; ``clone``
    # uses APFS / btrfs reflinks (CoW); ``copy`` is the safe fallback.
    link_mode: Literal["hardlink", "clone", "symlink", "copy"] = "hardlink"

    # Packages to install into a freshly bootstrapped venv that has no
    # ``pyproject.toml`` / ``requirements.txt``. Empty means "create the venv
    # but install nothing extra; agent will add packages on demand".
    baseline_requirements: list[str] = Field(default_factory=list)

    # Pinned interpreter version, e.g. ``3.11``, ``3.12``, ``3.11.10``.
    # Empty means "let uv pick a compatible interpreter, downloading a
    # standalone build if necessary".
    python_version: str = ""


class ExecToolConfig(Base):
    """Shell exec tool configuration."""

    enable: bool = True
    timeout: int = 60
    path_append: str = ""
    sandbox: str = ""  # sandbox backend: "" (none) or "bwrap"
    python: PythonRuntimeConfig = Field(default_factory=PythonRuntimeConfig)


class MCPServerConfig(Base):
    """MCP server connection configuration (stdio or HTTP)."""

    type: Literal["stdio", "sse", "streamableHttp"] | None = None  # auto-detected if omitted
    command: str = ""  # Stdio: command to run (e.g. "npx")
    args: list[str] = Field(default_factory=list)  # Stdio: command arguments
    env: dict[str, str] = Field(default_factory=dict)  # Stdio: extra env vars
    url: str = ""  # HTTP/SSE: endpoint URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP/SSE: custom headers
    tool_timeout: int = 30  # seconds before a tool call is cancelled
    enabled_tools: list[str] = Field(default_factory=lambda: ["*"])  # Only register these tools; accepts raw MCP names or wrapped mcp_<server>_<tool> names; ["*"] = all tools; [] = no tools

class ToolsConfig(Base):
    """Tools configuration."""

    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    restrict_to_workspace: bool = False  # restrict all tool access to workspace directory
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    ssrf_whitelist: list[str] = Field(default_factory=list)  # CIDR ranges to exempt from SSRF blocking (e.g. ["100.64.0.0/10"] for Tailscale)


class Config(BaseSettings):
    """Root configuration for mira."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)

    @model_validator(mode="after")
    def _enable_channel_builtin_access(self) -> "Config":
        object.__setattr__(self.channels, "_enable_builtin_access", True)
        for name in ChannelsConfig.builtin_channel_names():
            self.channels._ensure_builtin(name)
        return self

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    def _match_provider(
        self, model: str | None = None
    ) -> tuple["ProviderConfig | None", str | None]:
        """Match provider config and its registry name. Returns (config, spec_name)."""
        from mira_engine.providers.registry import PROVIDERS, find_by_name

        def _normalized_provider_name(value: str | None) -> str | None:
            if not value:
                return None
            normalized = value.replace("-", "_")
            chars: list[str] = []
            for i, ch in enumerate(normalized):
                if ch.isupper() and i > 0 and normalized[i - 1] != "_":
                    chars.append("_")
                chars.append(ch.lower())
            return "".join(chars)

        forced = self.agents.defaults.provider
        if forced != "auto":
            forced_normalized = _normalized_provider_name(forced)
            spec = find_by_name(forced_normalized or forced)
            if spec:
                p = getattr(self.providers, spec.name, None)
                return (p, spec.name) if p else (None, None)
            return None, None

        model_lower = (model or self.agents.defaults.model).lower()
        model_normalized = model_lower.replace("-", "_")
        model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
        normalized_prefix = model_prefix.replace("-", "_")

        def _kw_matches(kw: str) -> bool:
            kw = kw.lower()
            return kw in model_lower or kw.replace("-", "_") in model_normalized

        # Explicit provider prefix wins — prevents `github-copilot/...codex` matching openai_codex.
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and model_prefix and normalized_prefix == spec.name:
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # Match by keyword (order follows PROVIDERS registry)
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and any(_kw_matches(kw) for kw in spec.keywords):
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # Fallback: configured local providers can route models without
        # provider-specific keywords (for example plain "llama3.2" on Ollama).
        # Prefer providers whose detect_by_base_keyword matches the configured api_base
        # (e.g. Ollama's "11434" in "http://localhost:11434") over plain registry order.
        local_fallback: tuple[ProviderConfig, str] | None = None
        for spec in PROVIDERS:
            if not spec.is_local:
                continue
            p = getattr(self.providers, spec.name, None)
            if not (p and p.api_base):
                continue
            if spec.detect_by_base_keyword and spec.detect_by_base_keyword in p.api_base:
                return p, spec.name
            if local_fallback is None:
                local_fallback = (p, spec.name)
        if local_fallback:
            return local_fallback

        # Fallback: gateways first, then others (follows registry order)
        # OAuth providers are NOT valid fallbacks — they require explicit model selection
        for spec in PROVIDERS:
            if spec.is_oauth:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and p.api_key:
                return p, spec.name
        return None, None

    def get_provider(self, model: str | None = None) -> ProviderConfig | None:
        """Get matched provider config (api_key, api_base, extra_headers). Falls back to first available."""
        p, _ = self._match_provider(model)
        return p

    def get_provider_name(self, model: str | None = None) -> str | None:
        """Get the registry name of the matched provider (e.g. "deepseek", "openrouter")."""
        _, name = self._match_provider(model)
        return name

    def get_api_key(self, model: str | None = None) -> str | None:
        """Get API key for the given model. Falls back to first available key."""
        p = self.get_provider(model)
        return p.api_key if p else None

    def get_api_base(self, model: str | None = None) -> str | None:
        """Get API base URL for the given model. Applies default URLs for gateway/local providers."""
        from mira_engine.providers.registry import find_by_name

        p, name = self._match_provider(model)
        if p and p.api_base:
            return p.api_base
        # Only gateways get a default api_base here. Standard providers
        # resolve their base URL from the registry in the provider constructor.
        if name:
            spec = find_by_name(name)
            if spec and (spec.is_gateway or spec.is_local) and spec.default_api_base:
                return spec.default_api_base
        return None

    model_config = ConfigDict(env_prefix="MIRA_", env_nested_delimiter="__")
