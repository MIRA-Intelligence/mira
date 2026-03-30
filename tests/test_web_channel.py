import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from medpilot.bus.events import OutboundMessage
from medpilot.bus.queue import MessageBus
from medpilot.channels.base import BaseChannel
from medpilot.channels import web as web_channel_mod
from medpilot.channels.web import PLAN_FILENAME, WebChannel, _load_ui_instructions
from medpilot.config.schema import WebChannelConfig
from medpilot.session.manager import SessionManager


def _minimal_base_init(self, config, bus) -> None:
    self.config = config
    self.bus = bus
    self._running = False


class _FakePart:
    def __init__(self, name: str, filename: str | None, chunks: list[bytes]) -> None:
        self.name = name
        self.filename = filename
        self._chunks = list(chunks)

    async def read_chunk(self) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    async def release(self) -> None:
        self._chunks.clear()


class _FakeMultipart:
    def __init__(self, parts: list[_FakePart]) -> None:
        self._parts = list(parts)

    async def next(self) -> _FakePart | None:
        if not self._parts:
            return None
        return self._parts.pop(0)


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


async def test_handle_plan_recovers_completed_experiment_from_outputs(web_channel: WebChannel) -> None:
    session = "PRJ-0001"
    project_dir = web_channel.projects_root / session
    (project_dir / "outputs" / "exp004").mkdir(parents=True)
    (project_dir / "outputs" / "exp004" / "results.json").write_text(
        json.dumps({"score": 0.95}),
        encoding="utf-8",
    )
    (project_dir / PLAN_FILENAME).write_text(
        json.dumps({
            "title": "demo",
            "core_question": "q",
            "status": "in_progress",
            "started_at": "2026-03-24T12:00:00Z",
            "current_experiment": "Exp003",
            "research": {},
            "experiments": [
                {"id": "Exp003", "title": "done", "status": "completed"},
                {"id": "Exp004", "title": "recover", "status": "pending"},
                {"id": "Exp005", "title": "next", "status": "pending"},
            ],
            "knowledge": [],
            "result": {},
        }),
        encoding="utf-8",
    )

    req = MagicMock(spec=web.Request)
    req.query = {"session_id": session}
    resp = await web_channel._handle_plan(req)

    assert resp.status == 200
    body = json.loads(resp.text)
    exp004 = body["experiments"][1]
    assert exp004["status"] == "completed"
    assert exp004["results"]["metrics"] == {"score": 0.95}
    assert exp004["results"]["artifacts"] == ["outputs/exp004/results.json"]
    assert body["current_experiment"] == "Exp005"


async def test_handle_history_returns_entries(web_channel: WebChannel) -> None:
    session_id = "PRJ-0001"
    project_dir = web_channel.projects_root / session_id
    project_dir.mkdir(parents=True)

    manager = SessionManager(project_dir)
    session = manager.get_or_create(f"web:{session_id}")
    session.messages = [
        {"role": "user", "content": "hello", "timestamp": "2026-03-26T10:00:00"},
        {
            "role": "assistant",
            "content": "Working on it",
            "timestamp": "2026-03-26T10:00:01",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": "{\"path\":\"x\"}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "write_file", "content": "ok", "timestamp": "2026-03-26T10:00:02"},
        {"role": "assistant", "content": "Done", "timestamp": "2026-03-26T10:00:03"},
    ]
    manager.save(session)

    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": session_id}
    resp = await web_channel._handle_history(req)

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["session_id"] == session_id
    assert body["entries"] == [
        {
            "id": f"history-{session_id}-0-user",
            "timestamp": "2026-03-26T10:00:00",
            "content": "hello",
            "type": "response",
            "metadata": {"_user": True},
        },
        {
            "id": f"history-{session_id}-1-assistant",
            "timestamp": "2026-03-26T10:00:01",
            "content": "Working on it",
            "type": "response",
            "metadata": {},
        },
        {
            "id": f"history-{session_id}-1-tool-0",
            "timestamp": "2026-03-26T10:00:01",
            "content": "write_file({\"path\":\"x\"})",
            "type": "tool_call",
            "metadata": {},
        },
        {
            "id": f"history-{session_id}-3-assistant",
            "timestamp": "2026-03-26T10:00:03",
            "content": "Done",
            "type": "response",
            "metadata": {},
        },
    ]


async def test_handle_history_missing_project_returns_empty(web_channel: WebChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "missing"}
    resp = await web_channel._handle_history(req)

    assert resp.status == 200
    assert json.loads(resp.text) == {"session_id": "missing", "entries": []}


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


async def test_handle_upload_project_files_invalid_multipart(web_channel: WebChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0001"}
    req.multipart = AsyncMock(side_effect=RuntimeError("bad form"))

    resp = await web_channel._handle_upload_project_files(req)
    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "expected multipart/form-data"}


async def test_handle_upload_project_files_missing_files(web_channel: WebChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0001"}
    req.multipart = AsyncMock(return_value=_FakeMultipart([
        _FakePart(name="metadata", filename="ignored.txt", chunks=[b"abc"]),
    ]))

    resp = await web_channel._handle_upload_project_files(req)
    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "no files uploaded"}


async def test_handle_upload_project_files_writes_data_files(web_channel: WebChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0001"}
    req.multipart = AsyncMock(return_value=_FakeMultipart([
        _FakePart(name="files", filename="sample.csv", chunks=[b"a,", b"b\n"]),
        _FakePart(name="files", filename="sample.csv", chunks=[b"c,d\n"]),
    ]))

    resp = await web_channel._handle_upload_project_files(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["session_id"] == "PRJ-0001"
    assert body["uploaded"] == [
        {"name": "sample.csv", "path": "data/sample.csv", "size": 4},
        {"name": "sample_1.csv", "path": "data/sample_1.csv", "size": 4},
    ]

    data_dir = web_channel.projects_root / "PRJ-0001" / "data"
    assert (data_dir / "sample.csv").read_bytes() == b"a,b\n"
    assert (data_dir / "sample_1.csv").read_bytes() == b"c,d\n"


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
