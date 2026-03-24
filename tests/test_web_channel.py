import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from radiologybot.bus.events import OutboundMessage
from radiologybot.bus.queue import MessageBus
from radiologybot.channels.base import BaseChannel
from radiologybot.channels import web as web_channel_mod
from radiologybot.channels.web import PLAN_FILENAME, WebChannel, _load_ui_instructions
from radiologybot.config.schema import WebChannelConfig


def _minimal_base_init(self, config, bus) -> None:
    self.config = config
    self.bus = bus
    self._running = False


@pytest.fixture
def web_channel(tmp_path: Path) -> WebChannel:
    config = MagicMock(spec=WebChannelConfig)
    bus = MagicMock(spec=MessageBus)
    with patch.object(BaseChannel, "__init__", _minimal_base_init):
        with patch.object(web_channel_mod, "_load_ui_instructions", return_value=""):
            ch = WebChannel(config, bus)
            ch.projects_root = tmp_path
            return ch


def test_load_ui_instructions_joins_present_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(web_channel_mod, "_ASSETS_DIR", tmp_path)
    (tmp_path / "AGENTS_UI.md").write_text("alpha", encoding="utf-8")
    (tmp_path / "SKILL_UI.md").write_text("beta", encoding="utf-8")
    assert _load_ui_instructions() == "alpha\n\n---\n\nbeta"


def test_load_ui_instructions_skips_missing_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(web_channel_mod, "_ASSETS_DIR", tmp_path)
    (tmp_path / "AGENTS_UI.md").write_text("only", encoding="utf-8")
    assert _load_ui_instructions() == "only"


async def test_handle_plan_no_session_id(web_channel: WebChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.query = {}
    resp = await web_channel._handle_plan(req)
    assert resp.status == 200
    assert json.loads(resp.text) is None


async def test_handle_plan_missing_file(web_channel: WebChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.query = {"session_id": "s1"}
    resp = await web_channel._handle_plan(req)
    assert resp.status == 200
    assert json.loads(resp.text) is None


async def test_handle_plan_returns_json(web_channel: WebChannel) -> None:
    session = "sess-a"
    plan_dir = web_channel.projects_root / session
    plan_dir.mkdir(parents=True)
    data = {"steps": [{"id": 1}]}
    (plan_dir / PLAN_FILENAME).write_text(json.dumps(data), encoding="utf-8")
    req = MagicMock(spec=web.Request)
    req.query = {"session_id": session}
    resp = await web_channel._handle_plan(req)
    assert resp.status == 200
    assert json.loads(resp.text) == data


async def test_handle_plan_invalid_json_returns_500(web_channel: WebChannel) -> None:
    session = "bad-json"
    plan_dir = web_channel.projects_root / session
    plan_dir.mkdir(parents=True)
    (plan_dir / PLAN_FILENAME).write_text("{", encoding="utf-8")
    req = MagicMock(spec=web.Request)
    req.query = {"session_id": session}
    resp = await web_channel._handle_plan(req)
    assert resp.status == 500
    body = json.loads(resp.text)
    assert "error" in body


async def test_handle_config_invalid_json(web_channel: WebChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.json = AsyncMock(side_effect=json.JSONDecodeError("msg", "", 0))
    resp = await web_channel._handle_config(req)
    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "invalid JSON"}


async def test_handle_config_updates_projects_root(web_channel: WebChannel, tmp_path: Path) -> None:
    new_root = tmp_path / "projects"
    new_root.mkdir()
    req = MagicMock(spec=web.Request)
    req.json = AsyncMock(return_value={"projects_root": str(new_root)})
    resp = await web_channel._handle_config(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["projects_root"] == str(new_root.resolve())
    assert web_channel.projects_root == new_root.resolve()


async def test_handle_config_unchanged_without_key(web_channel: WebChannel, tmp_path: Path) -> None:
    web_channel.projects_root = tmp_path
    req = MagicMock(spec=web.Request)
    req.json = AsyncMock(return_value={})
    resp = await web_channel._handle_config(req)
    assert resp.status == 200
    assert json.loads(resp.text)["projects_root"] == str(tmp_path)


async def test_send_delivers_json_to_open_socket(web_channel: WebChannel) -> None:
    ws = MagicMock()
    ws.closed = False
    ws.send_json = AsyncMock()
    web_channel._clients["sid-1"] = ws
    msg = OutboundMessage(
        channel="web",
        chat_id="sid-1",
        content="hello",
        media=["u1"],
        metadata={"k": "v"},
    )
    await web_channel.send(msg)
    ws.send_json.assert_awaited_once()
    payload = ws.send_json.await_args.args[0]
    assert payload == {
        "type": "response",
        "session_id": "sid-1",
        "content": "hello",
        "media": ["u1"],
        "metadata": {"k": "v"},
    }


async def test_send_progress_type(web_channel: WebChannel) -> None:
    ws = MagicMock()
    ws.closed = False
    ws.send_json = AsyncMock()
    web_channel._clients["x"] = ws
    msg = OutboundMessage(
        channel="web",
        chat_id="x",
        content="…",
        metadata={"_progress": True},
    )
    await web_channel.send(msg)
    assert ws.send_json.await_args.args[0]["type"] == "progress"


async def test_send_no_client_noop(web_channel: WebChannel) -> None:
    msg = OutboundMessage(channel="web", chat_id="missing", content="x")
    await web_channel.send(msg)


async def test_send_closed_socket_noop(web_channel: WebChannel) -> None:
    ws = MagicMock()
    ws.closed = True
    ws.send_json = AsyncMock()
    web_channel._clients["gone"] = ws
    await web_channel.send(OutboundMessage(channel="web", chat_id="gone", content="x"))
    ws.send_json.assert_not_called()


async def test_send_send_json_failure_swallowed(web_channel: WebChannel) -> None:
    ws = MagicMock()
    ws.closed = False
    ws.send_json = AsyncMock(side_effect=RuntimeError("broken"))
    web_channel._clients["err"] = ws
    await web_channel.send(OutboundMessage(channel="web", chat_id="err", content="x"))
