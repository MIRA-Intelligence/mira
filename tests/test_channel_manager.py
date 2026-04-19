from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass
from types import SimpleNamespace

from medpilot.bus.events import OutboundMessage
from medpilot.bus.queue import MessageBus
from medpilot.channels.base import BaseChannel
from medpilot.channels.manager import ChannelManager
from medpilot.config.schema import Config


class _DummyChannel(BaseChannel):
    def __init__(self, config, bus, **kwargs):
        super().__init__(config, bus)
        self.started = False
        self.stopped = False
        self.sent = []
        self.fail_on_stop = False

    async def start(self) -> None:
        self.started = True
        self._running = True

    async def stop(self) -> None:
        if self.fail_on_stop:
            raise RuntimeError("stop failed")
        self.stopped = True
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)


def _install_channel_module(monkeypatch, module_name: str, cls_name: str) -> None:
    mod = types.ModuleType(module_name)
    setattr(mod, cls_name, _DummyChannel)
    monkeypatch.setitem(sys.modules, module_name, mod)


def _enable_all_channels(cfg: Config) -> None:
    for name in (
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
        "web",
    ):
        ch = getattr(cfg.channels, name)
        ch.enabled = True
        if hasattr(ch, "allow_from"):
            ch.allow_from = ["*"]


def test_init_channels_registers_enabled_channels(monkeypatch) -> None:
    for module_name, cls_name in (
        ("medpilot.channels.telegram", "TelegramChannel"),
        ("medpilot.channels.whatsapp", "WhatsAppChannel"),
        ("medpilot.channels.discord", "DiscordChannel"),
        ("medpilot.channels.feishu", "FeishuChannel"),
        ("medpilot.channels.mochat", "MochatChannel"),
        ("medpilot.channels.dingtalk", "DingTalkChannel"),
        ("medpilot.channels.email", "EmailChannel"),
        ("medpilot.channels.slack", "SlackChannel"),
        ("medpilot.channels.qq", "QQChannel"),
        ("medpilot.channels.matrix", "MatrixChannel"),
        ("medpilot.channels.web", "WebChannel"),
    ):
        _install_channel_module(monkeypatch, module_name, cls_name)

    cfg = Config()
    _enable_all_channels(cfg)
    mgr = ChannelManager(cfg, MessageBus())
    assert set(mgr.enabled_channels) == {
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
        "web",
    }


def test_validate_allow_from_rejects_empty_lists() -> None:
    mgr = ChannelManager.__new__(ChannelManager)
    mgr.channels = {"telegram": SimpleNamespace(config=SimpleNamespace(allow_from=[]))}
    try:
        mgr._validate_allow_from()
        assert False, "Expected SystemExit"
    except SystemExit as exc:
        assert "empty allowFrom" in str(exc)


async def test_start_all_and_stop_all_with_channels(monkeypatch) -> None:
    _install_channel_module(monkeypatch, "medpilot.channels.telegram", "TelegramChannel")
    cfg = Config()
    cfg.channels.telegram.enabled = True
    cfg.channels.telegram.allow_from = ["*"]
    bus = MessageBus()
    mgr = ChannelManager(cfg, bus)

    await mgr.start_all()
    assert mgr.get_channel("telegram").started is True
    assert mgr._dispatch_task is not None

    await mgr.stop_all()
    assert mgr.get_channel("telegram").stopped is True


async def test_start_all_without_channels_returns_early() -> None:
    cfg = Config()
    bus = MessageBus()
    mgr = ChannelManager(cfg, bus)
    mgr.channels = {}
    await mgr.start_all()
    assert mgr._dispatch_task is None


async def test_dispatch_outbound_filters_progress_messages() -> None:
    cfg = Config()
    cfg.channels.send_progress = False
    cfg.channels.send_tool_hints = False
    bus = MessageBus()
    mgr = ChannelManager(cfg, bus)
    ch = _DummyChannel(SimpleNamespace(allow_from=["*"]), bus)
    mgr.channels = {"web": ch}

    task = asyncio.create_task(mgr._dispatch_outbound())
    await bus.publish_outbound(OutboundMessage("web", "x", "normal"))
    await bus.publish_outbound(OutboundMessage("web", "x", "progress", metadata={"_progress": True}))
    await bus.publish_outbound(
        OutboundMessage("web", "x", "hint", metadata={"_progress": True, "_tool_hint": True})
    )
    await asyncio.sleep(0.1)
    task.cancel()
    await task

    assert [m.content for m in ch.sent] == ["normal"]


async def test_dispatch_outbound_handles_unknown_channel_and_send_errors() -> None:
    cfg = Config()
    bus = MessageBus()
    mgr = ChannelManager(cfg, bus)
    bad = _DummyChannel(SimpleNamespace(allow_from=["*"]), bus)

    async def _boom(_msg):
        raise RuntimeError("send fail")

    bad.send = _boom
    mgr.channels = {"web": bad}

    task = asyncio.create_task(mgr._dispatch_outbound())
    await bus.publish_outbound(OutboundMessage("web", "x", "one"))
    await bus.publish_outbound(OutboundMessage("missing", "x", "two"))
    await asyncio.sleep(0.1)
    task.cancel()
    await task


async def test_stop_all_continues_when_channel_stop_fails() -> None:
    cfg = Config()
    bus = MessageBus()
    mgr = ChannelManager(cfg, bus)
    bad = _DummyChannel(SimpleNamespace(allow_from=["*"]), bus)
    bad.fail_on_stop = True
    mgr.channels = {"web": bad}
    mgr._dispatch_task = asyncio.create_task(asyncio.sleep(5))
    await mgr.stop_all()


def test_status_and_get_channel_helpers() -> None:
    cfg = Config()
    bus = MessageBus()
    mgr = ChannelManager(cfg, bus)
    ch = _DummyChannel(SimpleNamespace(allow_from=["*"]), bus)
    ch._running = True
    mgr.channels = {"web": ch}

    assert mgr.get_channel("web") is ch
    assert mgr.get_channel("missing") is None
    assert mgr.get_status() == {"web": {"enabled": True, "running": True}}
