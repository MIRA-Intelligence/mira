import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from medpilot import __version__
from medpilot.agent import skill_plugins as skill_plugins_mod
from medpilot.bus.events import OutboundMessage
from medpilot.bus.queue import MessageBus
from medpilot.channels import web as web_channel_mod
from medpilot.channels.base import BaseChannel
from medpilot.channels.web import (
    _API_CONTRACT_VERSION,
    PLAN_FILENAME,
    WebChannel,
    _load_ui_instructions,
    _normalize_agent_profile,
)
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


def _create_plugin_source(base: Path, plugin_id: str = "plugin-pack") -> Path:
    src = base / "plugin-src"
    (src / "skills" / "writer").mkdir(parents=True, exist_ok=True)
    (src / "skills" / "writer" / "SKILL.md").write_text("# Writer Skill", encoding="utf-8")
    (src / "plugin.json").write_text(
        json.dumps({
            "id": plugin_id,
            "version": "0.1.0",
            "skills": [{"id": "writer", "path": "skills/writer"}],
        }),
        encoding="utf-8",
    )
    return src


@pytest.fixture
def web_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WebChannel:
    config = MagicMock(spec=WebChannelConfig)
    bus = MagicMock(spec=MessageBus)
    global_workspace = tmp_path / "global-workspace"
    global_workspace.mkdir(parents=True)
    monkeypatch.setattr(skill_plugins_mod, "get_workspace_path", lambda _workspace: global_workspace)
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


def test_normalize_agent_profile_accepts_known_values() -> None:
    assert _normalize_agent_profile("engineer") == "engineer"
    assert _normalize_agent_profile("default") == "default"
    assert _normalize_agent_profile("research") == "research"


def test_normalize_agent_profile_falls_back_to_default() -> None:
    assert _normalize_agent_profile("unknown") == "default"
    assert _normalize_agent_profile(None) == "default"


async def test_handle_health_returns_machine_readable_payload(web_channel: WebChannel) -> None:
    web_channel._running = True
    web_channel._clients = {"s1": MagicMock(closed=False)}

    req = MagicMock(spec=web.Request)
    resp = await web_channel._handle_health(req)

    assert resp.status == 200
    assert json.loads(resp.text) == {
        "status": "ok",
        "service": "medpilot-gateway",
        "channel": "web",
        "running": True,
        "connected_clients": 1,
    }


async def test_handle_version_returns_contract_payload(web_channel: WebChannel) -> None:
    req = MagicMock(spec=web.Request)
    resp = await web_channel._handle_version(req)
    body = json.loads(resp.text)

    assert resp.status == 200
    assert body["service"] == "medpilot-gateway"
    assert body["agent_version"] == __version__
    assert body["api_contract"] == _API_CONTRACT_VERSION
    assert isinstance(body["uptime_seconds"], int)
    assert body["uptime_seconds"] >= 0


def test_audit_writes_global_and_project_logs(web_channel: WebChannel) -> None:
    session_id = "PRJ-LOG"
    project_dir = web_channel.projects_root / session_id
    project_dir.mkdir(parents=True)

    web_channel._audit(
        source="ui",
        action="ws_message_received",
        session_id=session_id,
        project_dir=project_dir,
        details={"content_preview": "hello"},
    )

    global_log = web_channel.projects_root / "logs" / "project_actions.jsonl"
    project_log = project_dir / ".medpilot" / "logs" / "actions.jsonl"
    assert global_log.is_file()
    assert project_log.is_file()

    global_entry = json.loads(global_log.read_text(encoding="utf-8").strip().splitlines()[-1])
    project_entry = json.loads(project_log.read_text(encoding="utf-8").strip().splitlines()[-1])

    assert global_entry["session_id"] == session_id
    assert global_entry["source"] == "ui"
    assert global_entry["action"] == "ws_message_received"
    assert project_entry == global_entry


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


async def test_handle_list_projects_only_returns_prj_with_meta(web_channel: WebChannel) -> None:
    (web_channel.projects_root / "PRJ-0001").mkdir(parents=True)
    (web_channel.projects_root / "PRJ-0002").mkdir(parents=True)
    (web_channel.projects_root / "skills").mkdir(parents=True)
    (web_channel.projects_root / "random-folder").mkdir(parents=True)

    req = MagicMock(spec=web.Request)
    resp = await web_channel._handle_list_projects(req)

    assert resp.status == 200
    body = json.loads(resp.text)
    ids = [item["id"] for item in body["projects"]]
    assert ids == ["PRJ-0001", "PRJ-0002"]
    assert [item["display_name"] for item in body["projects"]] == ["PRJ-0001", "PRJ-0002"]
    assert all(item["has_meta"] for item in body["projects"])

    meta_file = web_channel.projects_root / "PRJ-0001" / ".medpilot" / "project.json"
    assert meta_file.is_file()
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta["id"] == "PRJ-0001"
    assert meta["display_name"] == "PRJ-0001"


async def test_handle_project_meta_updates_display_name(web_channel: WebChannel) -> None:
    project_dir = web_channel.projects_root / "PRJ-0001"
    project_dir.mkdir(parents=True)

    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0001"}
    req.json = AsyncMock(return_value={"display_name": "Lung CT baseline"})
    resp = await web_channel._handle_project_meta(req)

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["display_name"] == "Lung CT baseline"

    meta_file = project_dir / ".medpilot" / "project.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta["display_name"] == "Lung CT baseline"


async def test_cors_allows_patch_method(web_channel: WebChannel) -> None:
    web_channel.config.cors_origins = ["*"]
    req = MagicMock(spec=web.Request)
    req.method = "OPTIONS"
    req.headers = {"Origin": "http://localhost:5173"}

    resp = await web_channel._cors_middleware(req, AsyncMock())
    assert resp.status == 204
    assert resp.headers["Access-Control-Allow-Methods"] == "GET, POST, PATCH, DELETE, OPTIONS"
    assert resp.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"


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


async def test_handle_project_artifact_serves_file(web_channel: WebChannel) -> None:
    project_dir = web_channel.projects_root / "PRJ-0001"
    artifact = project_dir / "experiments" / "exp005" / "roc_pr_curves.png"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"png")

    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0001"}
    req.query = {"path": "experiments/exp005/roc_pr_curves.png"}

    resp = await web_channel._handle_project_artifact(req)
    assert isinstance(resp, web.FileResponse)
    assert resp.status == 200
    assert Path(resp._path) == artifact


async def test_handle_project_artifact_blocks_traversal(web_channel: WebChannel, tmp_path: Path) -> None:
    project_dir = web_channel.projects_root / "PRJ-0001"
    project_dir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")

    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0001"}
    req.query = {"path": "../outside.txt"}

    resp = await web_channel._handle_project_artifact(req)
    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "invalid artifact path"}


async def test_skill_plugin_install_from_directory_and_list(web_channel: WebChannel, tmp_path: Path) -> None:
    src = _create_plugin_source(tmp_path)
    install_req = MagicMock(spec=web.Request)
    install_req.match_info = {"session_id": "PRJ-0001"}
    install_req.headers = {"Content-Type": "application/json"}
    install_req.json = AsyncMock(return_value={"path": str(src)})

    install_resp = await web_channel._handle_skill_plugins_install(install_req)
    assert install_resp.status == 200
    body = json.loads(install_resp.text)
    assert body["installed"]["id"] == "plugin-pack"

    list_req = MagicMock(spec=web.Request)
    list_req.match_info = {"session_id": "PRJ-0001"}
    list_resp = await web_channel._handle_skill_plugins_list(list_req)
    assert list_resp.status == 200
    list_body = json.loads(list_resp.text)
    assert {p["id"] for p in list_body["plugins"]} >= {"builtin-skills", "plugin-pack"}


async def test_skill_plugin_install_from_zip(web_channel: WebChannel, tmp_path: Path) -> None:
    src = _create_plugin_source(tmp_path, plugin_id="zip-pack")
    zip_path = tmp_path / "zip-pack.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for item in src.rglob("*"):
            if item.is_file():
                zf.write(item, item.relative_to(src))

    zip_bytes = zip_path.read_bytes()
    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0001"}
    req.headers = {"Content-Type": "multipart/form-data; boundary=fake"}
    req.multipart = AsyncMock(return_value=_FakeMultipart([
        _FakePart(name="zip", filename="zip-pack.zip", chunks=[zip_bytes]),
    ]))

    resp = await web_channel._handle_skill_plugins_install(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["installed"]["id"] == "zip-pack"


async def test_skill_plugin_install_from_zip_without_manifest(web_channel: WebChannel, tmp_path: Path) -> None:
    src = tmp_path / "no-manifest-pack"
    (src / "research" / "finder").mkdir(parents=True, exist_ok=True)
    (src / "research" / "finder" / "SKILL.md").write_text(
        "---\nname: Finder\n---\n\n# skill",
        encoding="utf-8",
    )
    zip_path = tmp_path / "no-manifest-pack.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for item in src.rglob("*"):
            if item.is_file():
                zf.write(item, item.relative_to(src))

    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0001"}
    req.headers = {"Content-Type": "multipart/form-data; boundary=fake"}
    req.multipart = AsyncMock(return_value=_FakeMultipart([
        _FakePart(name="zip", filename="no-manifest-pack.zip", chunks=[zip_path.read_bytes()]),
    ]))

    resp = await web_channel._handle_skill_plugins_install(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["installed"]["id"] == "no-manifest-pack"


async def test_skill_plugin_toggle_and_uninstall(web_channel: WebChannel, tmp_path: Path) -> None:
    src = _create_plugin_source(tmp_path)
    install_req = MagicMock(spec=web.Request)
    install_req.match_info = {"session_id": "PRJ-0001"}
    install_req.headers = {"Content-Type": "application/json"}
    install_req.json = AsyncMock(return_value={"path": str(src)})
    await web_channel._handle_skill_plugins_install(install_req)

    toggle_req = MagicMock(spec=web.Request)
    toggle_req.match_info = {"session_id": "PRJ-0001"}
    toggle_req.json = AsyncMock(return_value={
        "scope": "global",
        "target_type": "skill",
        "plugin_id": "plugin-pack",
        "target_id": "writer",
        "enabled": False,
    })
    toggle_resp = await web_channel._handle_skill_plugins_state(toggle_req)
    assert toggle_resp.status == 200
    toggle_body = json.loads(toggle_resp.text)
    plugin_pack = next(item for item in toggle_body["plugins"] if item["id"] == "plugin-pack")
    writer = next(item for item in plugin_pack["skills"] if item["id"] == "writer")
    assert writer["enabled"]["effective"] is False

    remove_req = MagicMock(spec=web.Request)
    remove_req.match_info = {"session_id": "PRJ-0001", "plugin_id": "plugin-pack"}
    remove_resp = await web_channel._handle_skill_plugins_uninstall(remove_req)
    assert remove_resp.status == 200
    remove_body = json.loads(remove_resp.text)
    assert [item["id"] for item in remove_body["plugins"]] == ["builtin-skills"]


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


async def test_send_writes_project_audit_entry(web_channel: WebChannel) -> None:
    session_id = "sid-log"
    project_dir = web_channel.projects_root / session_id
    project_dir.mkdir(parents=True)

    ws = MagicMock()
    ws.closed = False
    ws.send_json = AsyncMock()
    web_channel._clients[session_id] = ws

    msg = OutboundMessage(
        channel="web",
        chat_id=session_id,
        content="running exp",
        metadata={"_progress": True, "_tool_hint": True},
    )
    await web_channel.send(msg)

    project_log = project_dir / ".medpilot" / "logs" / "actions.jsonl"
    assert project_log.is_file()
    entry = json.loads(project_log.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert entry["source"] == "agent"
    assert entry["action"] == "ws_outbound_sent"
    assert entry["details"]["type"] == "progress"
    assert entry["details"]["tool_hint"] is True


async def test_send_audit_only_skill_event_writes_project_log(web_channel: WebChannel) -> None:
    session_id = "sid-skill-log"
    project_dir = web_channel.projects_root / session_id
    project_dir.mkdir(parents=True)

    msg = OutboundMessage(
        channel="web",
        chat_id=session_id,
        content="",
        metadata={
            "_audit_only": True,
            "_audit_event": "skill_invoked",
            "_audit_details": {
                "tool": "read_file",
                "skill_name": "scientific-method",
                "path": "/tmp/skills/research/scientific-method/SKILL.md",
            },
        },
    )
    await web_channel.send(msg)

    project_log = project_dir / ".medpilot" / "logs" / "actions.jsonl"
    assert project_log.is_file()
    entry = json.loads(project_log.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert entry["source"] == "agent"
    assert entry["action"] == "skill_invoked"
    assert entry["details"]["tool"] == "read_file"
    assert entry["details"]["skill_name"] == "scientific-method"


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
