"""Channel manager for coordinating chat channels."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from loguru import logger

from mira_engine.bus.events import OutboundMessage
from mira_engine.bus.queue import MessageBus
from mira_engine.channels.base import BaseChannel
from mira_engine.config.schema import Config
from mira_engine.utils.restart import (
    consume_restart_notice_from_env,
    format_restart_completed_message,
)


class ChannelManager:
    """Manage channel lifecycle and outbound delivery."""

    def __init__(
        self,
        config: Config,
        bus: MessageBus,
        *,
        on_ui_runtime_config_updated: Callable[[Config, Path], Awaitable[None]] | None = None,
    ):
        self.config = config
        self.bus = bus
        self.on_ui_runtime_config_updated = on_ui_runtime_config_updated
        self.channels: dict[str, BaseChannel] = {}
        self._dispatch_task: asyncio.Task | None = None
        self._init_channels()
        self._notify_restart_done_if_needed()

    @staticmethod
    def _to_ns(value: Any) -> Any:
        import re

        from pydantic import BaseModel

        def to_snake(name: str) -> str:
            name = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
            return re.sub("([a-z0-9])([A-Z])", r"\1_\2", name).lower()

        if is_dataclass(value):
            return SimpleNamespace(**asdict(value))

        d = None
        if isinstance(value, BaseModel):
            d = value.model_dump()
        elif isinstance(value, dict):
            d = value

        if d is not None:
            ns_dict: dict[str, Any] = {}
            for k, v in d.items():
                ns_dict[k] = v
                snake_k = to_snake(k)
                if snake_k != k:
                    ns_dict.setdefault(snake_k, v)
            return SimpleNamespace(**ns_dict)

        return value

    @staticmethod
    def _config_value(config: Any, key: str, default: Any = None) -> Any:
        if isinstance(config, dict):
            aliases = (key, key.replace("_", "-"), key.replace("_", ""))
            camel = key.split("_")
            camel_key = camel[0] + "".join(part.capitalize() for part in camel[1:])
            for k in (*aliases, camel_key):
                if k in config:
                    return config[k]
            return default
        return getattr(config, key, default)

    def _iter_channel_sections(self) -> dict[str, Any]:
        channels = self.config.channels
        sections: dict[str, Any] = {}
        builtin_names = ("telegram", "whatsapp", "discord", "feishu", "mochat", "dingtalk", "email", "slack", "qq", "matrix", "ui")
        for name in builtin_names:
            if hasattr(channels, name):
                sections[name] = getattr(channels, name)
        extras = getattr(channels, "model_extra", None) or {}
        for name, section in extras.items():
            if name == "web" and "ui" in sections:
                # Already migrated by the config loader; ignore stale alias.
                continue
            if name not in sections:
                sections[name] = section
        return sections

    def _init_channels(self) -> None:
        from mira_engine.channels.registry import discover_all

        providers = discover_all()
        for name, cls in providers.items():
            section = self._iter_channel_sections().get(name)
            if section is None:
                continue
            enabled = bool(self._config_value(section, "enabled", False))
            if not enabled:
                continue
            try:
                kwargs: dict[str, Any] = {}
                if name in {"telegram", "feishu"}:
                    kwargs["groq_api_key"] = getattr(self.config.providers.groq, "api_key", "")
                if name == "ui":
                    kwargs["workspace"] = self.config.workspace_path
                    kwargs["bind_host"] = self.config.gateway.host
                    kwargs["bind_port"] = self.config.gateway.port
                    kwargs["on_runtime_config_updated"] = self.on_ui_runtime_config_updated
                self.channels[name] = cls(self._to_ns(section), self.bus, **kwargs)
                logger.info("{} channel enabled", name)
            except ImportError as e:
                logger.warning("{} channel not available: {}", name, e)
            except Exception as e:
                logger.warning("Failed to initialize {} channel: {}", name, e)

        self._validate_allow_from()

    def _validate_allow_from(self) -> None:
        for name, ch in self.channels.items():
            if getattr(ch.config, "allow_from", None) == []:
                raise SystemExit(
                    f'Error: "{name}" has empty allowFrom (denies all). '
                    f'Set ["*"] to allow everyone, or add specific user IDs.'
                )

    async def _start_channel(self, name: str, channel: BaseChannel) -> None:
        try:
            await channel.start()
        except Exception as e:
            logger.error("Failed to start channel {}: {}", name, e)

    async def start_all(self) -> None:
        if not self.channels:
            logger.warning("No channels enabled")
            return

        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())
        tasks = [asyncio.create_task(self._start_channel(name, channel)) for name, channel in self.channels.items()]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop_all(self) -> None:
        logger.info("Stopping all channels...")

        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass

        for name, channel in self.channels.items():
            try:
                await channel.stop()
                logger.info("Stopped {} channel", name)
            except Exception as e:
                logger.error("Error stopping {}: {}", name, e)

    def _coalesce_stream_deltas(self, first: OutboundMessage) -> tuple[OutboundMessage, list[OutboundMessage]]:
        if not first.metadata.get("_stream_delta") or first.metadata.get("_stream_end"):
            return first, []

        merged_content = first.content
        merged_metadata = dict(first.metadata)
        pending: list[OutboundMessage] = []

        q = self.bus.outbound
        while True:
            try:
                nxt = q.get_nowait()
            except asyncio.QueueEmpty:
                break

            same_stream = (
                nxt.channel == first.channel
                and nxt.chat_id == first.chat_id
                and nxt.metadata.get("_stream_delta")
                and nxt.metadata.get("_stream_id") == first.metadata.get("_stream_id")
            )
            if same_stream and not nxt.metadata.get("_stream_end"):
                merged_content += nxt.content
                continue
            if same_stream and nxt.metadata.get("_stream_end"):
                merged_content += nxt.content
                merged_metadata.update(nxt.metadata)
                break

            pending.append(nxt)
            break

        return (
            OutboundMessage(
                channel=first.channel,
                chat_id=first.chat_id,
                content=merged_content,
                reply_to=first.reply_to,
                media=first.media,
                metadata=merged_metadata,
            ),
            pending,
        )

    async def _send_with_retry(self, channel: BaseChannel, msg: OutboundMessage) -> None:
        if msg.metadata.get("_streamed"):
            return

        retries_cfg = getattr(self.config.channels, "send_max_retries", 3)
        attempts = max(1, int(retries_cfg))
        for i in range(attempts):
            try:
                if msg.metadata.get("_stream_delta"):
                    await channel.send_delta(msg.chat_id, msg.content, msg.metadata)
                else:
                    await channel.send(msg)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if i >= attempts - 1:
                    logger.error("Error sending to {} after {} attempts: {}", msg.channel, attempts, e)
                    return
                try:
                    await asyncio.sleep(0.5 * (2 ** i))
                except asyncio.CancelledError:
                    raise

    def _notify_restart_done_if_needed(self) -> None:
        notice = consume_restart_notice_from_env()
        if not notice:
            return
        channel = self.channels.get(notice.channel)
        if not channel:
            return
        msg = OutboundMessage(
            channel=notice.channel,
            chat_id=notice.chat_id,
            content=format_restart_completed_message(notice.started_at_raw),
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._send_with_retry(channel, msg))

    async def _dispatch_outbound(self) -> None:
        logger.info("Outbound dispatcher started")
        pending: list[OutboundMessage] = []

        while True:
            try:
                msg = pending.pop(0) if pending else await asyncio.wait_for(self.bus.consume_outbound(), timeout=1.0)

                if msg.metadata.get("_progress"):
                    if msg.metadata.get("_activity_ping"):
                        if msg.channel != "ui":
                            continue
                    elif (
                        msg.metadata.get("_tool_hint")
                        and msg.channel != "ui"
                        and not self.config.channels.send_tool_hints
                    ):
                        continue
                    elif not msg.metadata.get("_tool_hint") and not self.config.channels.send_progress:
                        continue

                if msg.metadata.get("_stream_delta") and not msg.metadata.get("_stream_end"):
                    msg, extra_pending = self._coalesce_stream_deltas(msg)
                    pending.extend(extra_pending)

                channel = self.channels.get(msg.channel)
                if channel:
                    await self._send_with_retry(channel, msg)
                else:
                    logger.warning("Unknown channel: {}", msg.channel)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    def get_channel(self, name: str) -> BaseChannel | None:
        return self.channels.get(name)

    def get_status(self) -> dict[str, Any]:
        return {name: {"enabled": True, "running": channel.is_running} for name, channel in self.channels.items()}

    @property
    def enabled_channels(self) -> list[str]:
        return list(self.channels.keys())
