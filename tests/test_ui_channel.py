import json
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from mira_engine import __version__
from mira_engine.agent import skill_plugins as skill_plugins_mod
from mira_engine.bus.events import OutboundMessage
from mira_engine.bus.queue import MessageBus
from mira_engine.channels import ui as ui_channel_mod
from mira_engine.channels.base import BaseChannel
from mira_engine.channels.ui import (
    _API_CONTRACT_VERSION,
    PLAN_FILENAME,
    UiChannel,
    _build_feedback_agent_text,
    _build_task_plan_guard_notice,
    _detect_guard_id_reassignments,
    _extract_plan_experiment_ids,
    _format_tool_call,
    _load_ui_instructions,
    _normalize_agent_profile,
    _normalize_contract_version,
    _normalize_loop_mode,
    _normalize_run_mode,
    _safe_upload_name,
    _stringify_history_content,
)
from mira_engine.config.schema import Config, UiChannelConfig
from mira_engine.session.manager import SessionManager


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
def ui_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> UiChannel:
    config = MagicMock(spec=UiChannelConfig)
    bus = MagicMock(spec=MessageBus)
    global_workspace = tmp_path / "global-workspace"
    global_workspace.mkdir(parents=True)
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(parents=True)
    monkeypatch.setattr(skill_plugins_mod, "get_workspace_path", lambda _workspace: global_workspace)
    monkeypatch.setattr(
        ui_channel_mod,
        "get_runtime_subdir",
        lambda name: (runtime_root / name).mkdir(parents=True, exist_ok=True) or (runtime_root / name),
    )
    with patch.object(BaseChannel, "__init__", _minimal_base_init):
        with patch.object(ui_channel_mod, "_load_ui_instructions", return_value=""):
            ch = UiChannel(config, bus, workspace=tmp_path)
            return ch


def test_load_ui_instructions_joins_present_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ui_channel_mod, "_ASSETS_DIR", tmp_path)
    (tmp_path / "AGENTS_UI.md").write_text("alpha", encoding="utf-8")
    (tmp_path / "SKILL_UI.md").write_text("beta", encoding="utf-8")
    assert _load_ui_instructions() == "alpha\n\n---\n\nbeta"


def test_load_ui_instructions_skips_missing_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ui_channel_mod, "_ASSETS_DIR", tmp_path)
    (tmp_path / "AGENTS_UI.md").write_text("only", encoding="utf-8")
    assert _load_ui_instructions() == "only"


def test_normalize_agent_profile_accepts_known_values() -> None:
    assert _normalize_agent_profile("engineer") == "engineer"
    assert _normalize_agent_profile("research") == "research"


def test_normalize_agent_profile_falls_back_to_default() -> None:
    assert _normalize_agent_profile("unknown") == "research"
    assert _normalize_agent_profile(None) == "research"


def test_normalize_contract_version_accepts_known_values() -> None:
    assert _normalize_contract_version(1) == 1
    assert _normalize_contract_version(2) == 2


def test_normalize_contract_version_falls_back_to_default() -> None:
    assert _normalize_contract_version(9) == 1
    assert _normalize_contract_version("2") == 1


def test_guard_id_reassignment_helpers(tmp_path: Path) -> None:
    project_dir = tmp_path / "PRJ-7001"
    project_dir.mkdir(parents=True)
    (project_dir / PLAN_FILENAME).write_text(
        json.dumps(
            {
                "title": "demo",
                "experiments": [
                    {"id": "Exp001"},
                    {"id": "Exp003"},
                    {"id": "Exp003"},
                ],
            }
        ),
        encoding="utf-8",
    )
    before_ids = _extract_plan_experiment_ids(project_dir)
    assert before_ids == ["Exp001", "Exp003", "Exp003"]

    after_ids = ["Exp001", "Exp003", "Exp004"]
    reassignments = _detect_guard_id_reassignments(before_ids, after_ids)
    assert reassignments == [(3, "Exp003", "Exp004")]

    notice = _build_task_plan_guard_notice(reassignments)
    assert notice is not None
    assert "Exp003 -> Exp004" in notice
    assert _build_task_plan_guard_notice([]) is None


async def test_handle_health_returns_machine_readable_payload(ui_channel: UiChannel) -> None:
    ui_channel._running = True
    ui_channel._clients = {"s1": MagicMock(closed=False)}

    req = MagicMock(spec=web.Request)
    resp = await ui_channel._handle_health(req)

    assert resp.status == 200
    assert json.loads(resp.text) == {
        "status": "ok",
        "service": "mira-gateway",
        "channel": "ui",
        "running": True,
        "connected_clients": 1,
    }


async def test_handle_version_returns_contract_payload(ui_channel: UiChannel) -> None:
    req = MagicMock(spec=web.Request)
    resp = await ui_channel._handle_version(req)
    body = json.loads(resp.text)

    assert resp.status == 200
    assert body["service"] == "mira-gateway"
    assert body["agent_version"] == __version__
    assert body["api_contract"] == _API_CONTRACT_VERSION
    assert isinstance(body["uptime_seconds"], int)
    assert body["uptime_seconds"] >= 0
    # Engine identity is surfaced so the desktop UI's fast-path health
    # probe can verify the live engine matches the bundled manifest
    # without invoking the slower `mira-engine status` CLI.
    assert "engine_sha256" in body
    assert "engine_manifest" in body
    assert "engine_executable" in body
    # `engine_sha256_at_boot` proves the engine snapshots its identity at
    # startup. The desktop UI uses its presence as a guarantee that
    # `engine_sha256` is a real boot snapshot (rather than a stale disk
    # re-read produced by an in-place DMG swap of the manifest file).
    assert "engine_sha256_at_boot" in body


async def test_handle_version_snapshots_identity_at_boot(
    monkeypatch, ui_channel: UiChannel
) -> None:
    """A DMG re-install overwrites the on-disk manifest in place. The
    running engine must keep reporting the identity it had *at boot* via
    /version, not whatever the new manifest now claims, otherwise the
    desktop UI would falsely believe the live engine already matches."""
    import mira_engine.channels.ui as ui_module

    captured_at_boot = ui_channel._engine_identity

    # Simulate the manifest being swapped out under the running engine
    # — a fresh `_current_engine_identity()` call would now return the
    # *new* SHA. The snapshot stored on the channel must shield us.
    monkeypatch.setattr(
        ui_module,
        "_current_engine_identity",
        lambda: {
            "engine_executable": "/opt/mira/mira-engine",
            "engine_manifest_path": "/opt/mira/mira-engine.manifest.json",
            "engine_manifest": {"sha256": "new-sha-after-dmg-swap"},
            "engine_sha256": "new-sha-after-dmg-swap",
        },
    )
    req = MagicMock(spec=web.Request)
    resp = await ui_channel._handle_version(req)
    body = json.loads(resp.text)

    # Boot-snapshot fields are stable across an in-place manifest swap.
    assert body["engine_sha256"] == captured_at_boot.get("engine_sha256")
    assert body["engine_sha256_at_boot"] == captured_at_boot.get("engine_sha256")
    assert body["engine_manifest"] == captured_at_boot.get("engine_manifest")
    assert body["engine_executable"] == captured_at_boot.get("engine_executable")


def test_audit_writes_global_and_project_logs(ui_channel: UiChannel) -> None:
    session_id = "PRJ-LOG"
    project_dir = ui_channel.projects_root / session_id
    project_dir.mkdir(parents=True)

    ui_channel._audit(
        source="ui",
        action="ws_message_received",
        session_id=session_id,
        project_dir=project_dir,
        details={"content_preview": "hello"},
    )

    global_log = ui_channel.projects_root / "logs" / "project_actions.jsonl"
    project_log = project_dir / ".mira" / "logs" / "actions.jsonl"
    assert global_log.is_file()
    assert project_log.is_file()

    global_entry = json.loads(global_log.read_text(encoding="utf-8").strip().splitlines()[-1])
    project_entry = json.loads(project_log.read_text(encoding="utf-8").strip().splitlines()[-1])

    assert global_entry["session_id"] == session_id
    assert global_entry["source"] == "ui"
    assert global_entry["action"] == "ws_message_received"
    assert project_entry == global_entry


async def test_handle_plan_no_session_id(ui_channel: UiChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.query = {}
    resp = await ui_channel._handle_plan(req)
    assert resp.status == 200
    assert json.loads(resp.text) is None


async def test_handle_plan_missing_file(ui_channel: UiChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.query = {"session_id": "s1"}
    resp = await ui_channel._handle_plan(req)
    assert resp.status == 200
    assert json.loads(resp.text) is None


async def test_handle_plan_contract_requires_session_id(ui_channel: UiChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.query = {}
    resp = await ui_channel._handle_plan_contract(req)
    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "session_id required"}


async def test_handle_plan_contract_returns_profile_rules(ui_channel: UiChannel) -> None:
    session = "PRJ-9015"
    project_dir = ui_channel.projects_root / session
    (project_dir / ".mira").mkdir(parents=True)
    (project_dir / ".mira" / "project.json").write_text(
        json.dumps({"agent_profile": "research", "contract_version": 2}),
        encoding="utf-8",
    )
    req = MagicMock(spec=web.Request)
    req.query = {"session_id": session}
    resp = await ui_channel._handle_plan_contract(req)
    body = json.loads(resp.text)

    assert resp.status == 200
    assert body["profile"] == "research"
    assert body["contract_version"] == 2
    assert "theoretical_proof" in body["required_completed_fields"]
    assert "evidence_refs" in body["required_falsify_fields"]


async def test_handle_plan_returns_json(ui_channel: UiChannel) -> None:
    session = "sess-a"
    plan_dir = ui_channel.projects_root / session
    plan_dir.mkdir(parents=True)
    data = {"steps": [{"id": 1}]}
    (plan_dir / PLAN_FILENAME).write_text(json.dumps(data), encoding="utf-8")
    req = MagicMock(spec=web.Request)
    req.query = {"session_id": session}
    resp = await ui_channel._handle_plan(req)
    assert resp.status == 200
    assert json.loads(resp.text) == data


async def test_handle_plan_invalid_json_returns_500(ui_channel: UiChannel) -> None:
    session = "bad-json"
    plan_dir = ui_channel.projects_root / session
    plan_dir.mkdir(parents=True)
    (plan_dir / PLAN_FILENAME).write_text("{", encoding="utf-8")
    req = MagicMock(spec=web.Request)
    req.query = {"session_id": session}
    resp = await ui_channel._handle_plan(req)
    assert resp.status == 500
    body = json.loads(resp.text)
    assert "error" in body


async def test_handle_plan_recovers_completed_experiment_from_outputs(ui_channel: UiChannel) -> None:
    session = "PRJ-0001"
    project_dir = ui_channel.projects_root / session
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
    resp = await ui_channel._handle_plan(req)

    assert resp.status == 200
    body = json.loads(resp.text)
    exp004 = body["experiments"][1]
    assert exp004["status"] == "completed"
    assert exp004["results"]["metrics"] == {"score": 0.95}
    assert exp004["results"]["artifacts"] == ["outputs/exp004/results.json"]
    assert body["current_experiment"] == "Exp005"


async def test_handle_plan_attaches_and_persists_completed_experiment_snapshot(
    ui_channel: UiChannel,
) -> None:
    session = "PRJ-9010"
    project_dir = ui_channel.projects_root / session
    project_dir.mkdir(parents=True)
    (project_dir / PLAN_FILENAME).write_text(
        json.dumps(
            {
                "title": "demo",
                "status": "completed",
                "experiments": [
                    {
                        "id": "Exp001",
                        "title": "baseline",
                        "status": "completed",
                        "results": {"findings": "initial findings"},
                        "conclusion": "initial conclusion",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    req = MagicMock(spec=web.Request)
    req.query = {"session_id": session}
    first_resp = await ui_channel._handle_plan(req)
    first_body = json.loads(first_resp.text)
    exp = first_body["experiments"][0]
    assert exp["snapshot"]["conclusion"] == "initial conclusion"
    assert exp["snapshot"]["results"]["findings"] == "initial findings"

    saved_snapshot = (
        project_dir / ".mira" / "snapshots" / "experiments" / "Exp001.json"
    )
    assert saved_snapshot.is_file()

    (project_dir / PLAN_FILENAME).write_text(
        json.dumps(
            {
                "title": "demo",
                "status": "in_progress",
                "experiments": [
                    {
                        "id": "Exp001",
                        "title": "baseline-updated",
                        "status": "completed",
                        "results": {"metrics": {"score": 0.1}},
                        "conclusion": "Recovered completed experiment artifacts from workspace.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    second_resp = await ui_channel._handle_plan(req)
    second_body = json.loads(second_resp.text)
    exp2 = second_body["experiments"][0]
    assert exp2["conclusion"] == "Recovered completed experiment artifacts from workspace."
    assert exp2["snapshot"]["conclusion"] == "initial conclusion"
    assert exp2["snapshot"]["results"]["findings"] == "initial findings"


async def test_handle_plan_recovers_snapshot_from_git_history_when_current_is_degraded(
    ui_channel: UiChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = "PRJ-9011"
    project_dir = ui_channel.projects_root / session
    project_dir.mkdir(parents=True)
    (project_dir / ".git").mkdir(parents=True)
    (project_dir / PLAN_FILENAME).write_text(
        json.dumps(
            {
                "title": "demo",
                "status": "in_progress",
                "experiments": [
                    {
                        "id": "Exp001",
                        "title": "degraded",
                        "status": "completed",
                        "results": {"metrics": {"score": 0.2}},
                        "conclusion": "Recovered completed experiment artifacts from workspace.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ui_channel,
        "_recover_snapshot_from_git_history",
        lambda *_args, **_kwargs: {
            "title": "historical",
            "results": {"findings": "from git history"},
            "conclusion": "historical conclusion",
            "captured_at": "2026-04-09T00:00:00Z",
            "source": "git:abc1234",
        },
    )

    req = MagicMock(spec=web.Request)
    req.query = {"session_id": session}
    resp = await ui_channel._handle_plan(req)
    body = json.loads(resp.text)
    exp = body["experiments"][0]
    assert exp["snapshot"]["conclusion"] == "historical conclusion"
    assert exp["snapshot"]["results"]["findings"] == "from git history"

async def test_handle_plan_lint_auto_fixes_structure(ui_channel: UiChannel) -> None:
    session = "PRJ-9001"
    project_dir = ui_channel.projects_root / session
    (project_dir / "experiments" / "exp001").mkdir(parents=True)
    (project_dir / "experiments" / "exp001" / "metrics.json").write_text(
        json.dumps({"overall_r2": 0.51}),
        encoding="utf-8",
    )
    (project_dir / PLAN_FILENAME).write_text(
        json.dumps(
            {
                "title": "demo",
                "status": "in_progress",
                "experiments": [{"id": "exp1", "status": "completed"}],
            }
        ),
        encoding="utf-8",
    )

    req = MagicMock(spec=web.Request)
    req.query = {"session_id": session}
    resp = await ui_channel._handle_plan_lint(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["ok"] is True
    assert body["fixed"] is True
    assert body["issues"] == []

    saved = json.loads((project_dir / PLAN_FILENAME).read_text(encoding="utf-8"))
    exp = saved["experiments"][0]
    assert exp["id"] == "Exp001"
    assert exp["results"]["metrics"] == {"overall_r2": 0.51}
    assert "experiments/exp001/metrics.json" in exp["results"]["artifacts"]


async def test_handle_plan_auto_fixes_duplicate_experiment_ids(
    ui_channel: UiChannel,
) -> None:
    session = "PRJ-9002"
    project_dir = ui_channel.projects_root / session
    project_dir.mkdir(parents=True)
    (project_dir / PLAN_FILENAME).write_text(
        json.dumps(
            {
                "title": "dup",
                "status": "in_progress",
                "experiments": [
                    {"id": "Exp003", "status": "completed", "conclusion": "done"},
                    {"id": "Exp003", "status": "pending"},
                    {"id": "Exp004", "status": "pending"},
                ],
            }
        ),
        encoding="utf-8",
    )

    req = MagicMock(spec=web.Request)
    req.query = {"session_id": session}
    resp = await ui_channel._handle_plan(req)

    assert resp.status == 200
    body = json.loads(resp.text)
    ids = [exp["id"] for exp in body["experiments"]]
    assert ids == ["Exp003", "Exp004", "Exp005"]
    assert len(ids) == len(set(ids))


async def test_handle_history_returns_entries(ui_channel: UiChannel) -> None:
    session_id = "PRJ-0001"
    project_dir = ui_channel.projects_root / session_id
    project_dir.mkdir(parents=True)

    manager = SessionManager(project_dir)
    session = manager.get_or_create(f"ui:{session_id}")
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
    resp = await ui_channel._handle_history(req)

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


async def test_handle_history_missing_project_returns_empty(ui_channel: UiChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "missing"}
    resp = await ui_channel._handle_history(req)

    assert resp.status == 200
    assert json.loads(resp.text) == {"session_id": "missing", "entries": []}


async def test_handle_history_merges_ui_chat_log_entries(ui_channel: UiChannel) -> None:
    session_id = "PRJ-0009"
    project_dir = ui_channel.projects_root / session_id
    project_dir.mkdir(parents=True)
    manager = SessionManager(project_dir)
    manager.append_ui_event(
        key=f"ui:{session_id}",
        role="user",
        content="from ui user",
        msg_type="response",
        metadata={"_user": True},
        timestamp="2026-03-26T10:00:00",
    )
    manager.append_ui_event(
        key=f"ui:{session_id}",
        role="assistant",
        content="from ui assistant",
        msg_type="response",
        metadata={},
        timestamp="2026-03-26T10:00:01",
    )

    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": session_id}
    resp = await ui_channel._handle_history(req)
    body = json.loads(resp.text)
    contents = [entry["content"] for entry in body["entries"]]
    assert "from ui user" in contents
    assert "from ui assistant" in contents


async def test_handle_history_uses_audit_fallback_when_session_sparse(ui_channel: UiChannel) -> None:
    session_id = "PRJ-0010"
    project_dir = ui_channel.projects_root / session_id
    project_dir.mkdir(parents=True)
    audit_file = project_dir / ".mira" / "logs" / "actions.jsonl"
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    audit_file.write_text(
        "\n".join(
            [
                json.dumps({
                    "timestamp": "2026-03-26T09:00:00",
                    "source": "ui",
                    "action": "ws_message_received",
                    "session_id": session_id,
                    "details": {"content_preview": "audit user msg"},
                }),
                json.dumps({
                    "timestamp": "2026-03-26T09:00:01",
                    "source": "agent",
                    "action": "ws_outbound_sent",
                    "session_id": session_id,
                    "details": {"type": "response", "content_preview": "audit assistant msg"},
                }),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": session_id}
    resp = await ui_channel._handle_history(req)
    body = json.loads(resp.text)
    contents = [entry["content"] for entry in body["entries"]]
    assert "audit user msg" in contents
    assert "audit assistant msg" in contents


async def test_handle_history_prefers_ui_entries_over_audit_preview_when_present(
    ui_channel: UiChannel,
) -> None:
    session_id = "PRJ-0011"
    project_dir = ui_channel.projects_root / session_id
    project_dir.mkdir(parents=True)

    manager = SessionManager(project_dir)
    manager.append_ui_event(
        key=f"ui:{session_id}",
        role="assistant",
        content="full assistant message",
        msg_type="response",
        metadata={},
        timestamp="2026-03-26T09:00:01",
    )

    audit_file = project_dir / ".mira" / "logs" / "actions.jsonl"
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    audit_file.write_text(
        json.dumps({
            "timestamp": "2026-03-26T09:00:01",
            "source": "agent",
            "action": "ws_outbound_sent",
            "session_id": session_id,
            "details": {"type": "response", "content_preview": "full assistant message"},
        })
        + "\n",
        encoding="utf-8",
    )

    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": session_id}
    resp = await ui_channel._handle_history(req)
    body = json.loads(resp.text)
    contents = [entry["content"] for entry in body["entries"]]
    assert contents.count("full assistant message") == 1


async def test_handle_history_uses_bound_project_dir_after_projects_root_change(
    ui_channel: UiChannel,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_root = tmp_path / "old-root"
    new_root = tmp_path / "new-root"
    session_id = "PRJ-4220"
    project_dir = old_root / session_id
    project_dir.mkdir(parents=True, exist_ok=True)

    ui_channel.projects_root = old_root.resolve()
    ui_channel._known_project_roots = {old_root.resolve()}
    ui_channel._persist_project_runtime_preferences(
        project_dir,
        run_mode="auto",
        agent_profile="research",
        contract_version=1,
        automation_policy=None,
    )
    SessionManager(project_dir).append_ui_event(
        key=f"ui:{session_id}",
        role="assistant",
        content="persisted in old root",
        msg_type="response",
        metadata={},
    )

    config = Config()
    monkeypatch.setattr(ui_channel_mod.config_loader, "load_config", lambda _path=None: config)
    monkeypatch.setattr(ui_channel_mod, "save_ui_runtime_update", lambda *_args, **_kwargs: None)

    config_req = MagicMock(spec=web.Request)
    config_req.json = AsyncMock(return_value={"projects_root": str(new_root)})
    config_resp = await ui_channel._handle_config(config_req)
    assert config_resp.status == 200
    assert ui_channel.projects_root == new_root.resolve()

    history_req = MagicMock(spec=web.Request)
    history_req.match_info = {"session_id": session_id}
    history_resp = await ui_channel._handle_history(history_req)
    assert history_resp.status == 200
    body = json.loads(history_resp.text)
    assert [entry["content"] for entry in body["entries"]] == ["persisted in old root"]


async def test_resolve_project_dir_prefers_current_root_for_duplicate_project_ids(
    ui_channel: UiChannel,
    tmp_path: Path,
) -> None:
    old_root = tmp_path / "old-root"
    new_root = tmp_path / "new-root"
    session_id = "PRJ-4222"
    old_project = old_root / session_id
    new_project = new_root / session_id
    old_project.mkdir(parents=True, exist_ok=True)
    new_project.mkdir(parents=True, exist_ok=True)

    ui_channel.projects_root = old_root.resolve()
    ui_channel._known_project_roots = {old_root.resolve()}
    assert ui_channel._resolve_project_dir(session_id) == old_project.resolve()

    ui_channel.projects_root = new_root.resolve()
    ui_channel._remember_projects_root(new_root)

    assert ui_channel._resolve_project_dir(session_id) == new_project.resolve()


async def test_send_drops_outbound_for_same_id_bound_to_different_project_dir(
    ui_channel: UiChannel,
    tmp_path: Path,
) -> None:
    old_root = tmp_path / "old-root"
    new_root = tmp_path / "new-root"
    session_id = "PRJ-4223"
    old_project = old_root / session_id
    new_project = new_root / session_id
    old_project.mkdir(parents=True, exist_ok=True)
    new_project.mkdir(parents=True, exist_ok=True)

    ws = MagicMock()
    ws.closed = False
    ws.send_json = AsyncMock()
    ui_channel._clients[session_id] = ws
    ui_channel._client_project_dirs[session_id] = new_project.resolve()

    await ui_channel.send(OutboundMessage(
        channel="ui",
        chat_id=session_id,
        content="old-root progress",
        metadata={"project_dir": str(old_project), "_progress": True},
    ))

    ws.send_json.assert_not_called()
    old_history = SessionManager(old_project).get_ui_history(f"ui:{session_id}")
    new_history = SessionManager(new_project).get_ui_history(f"ui:{session_id}")
    assert [entry["content"] for entry in old_history] == ["old-root progress"]
    assert new_history == []


async def test_project_dir_index_survives_channel_restart_after_root_change(
    ui_channel: UiChannel,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_root = tmp_path / "old-root"
    new_root = tmp_path / "new-root"
    session_id = "PRJ-4221"
    project_dir = old_root / session_id
    artifact = project_dir / "results" / "report.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("hello", encoding="utf-8")

    ui_channel.projects_root = old_root.resolve()
    ui_channel._known_project_roots = {old_root.resolve()}
    ui_channel._persist_project_runtime_preferences(
        project_dir,
        run_mode="auto",
        agent_profile="research",
        contract_version=1,
        automation_policy=None,
    )

    config = Config()
    monkeypatch.setattr(ui_channel_mod.config_loader, "load_config", lambda _path=None: config)
    monkeypatch.setattr(ui_channel_mod, "save_ui_runtime_update", lambda *_args, **_kwargs: None)

    config_req = MagicMock(spec=web.Request)
    config_req.json = AsyncMock(return_value={"projects_root": str(new_root)})
    config_resp = await ui_channel._handle_config(config_req)
    assert config_resp.status == 200

    with patch.object(BaseChannel, "__init__", _minimal_base_init):
        with patch.object(ui_channel_mod, "_load_ui_instructions", return_value=""):
            restarted = UiChannel(
                MagicMock(spec=UiChannelConfig),
                MagicMock(spec=MessageBus),
                workspace=new_root,
            )

    artifact_req = MagicMock(spec=web.Request)
    artifact_req.match_info = {"session_id": session_id}
    artifact_req.query = {"path": "results/report.txt"}
    artifact_resp = await restarted._handle_project_artifact(artifact_req)
    assert isinstance(artifact_resp, web.FileResponse)
    assert artifact_resp.status == 200
    assert Path(artifact_resp._path) == artifact


async def test_handle_config_root_change_closes_existing_ws_bindings(
    ui_channel: UiChannel,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_root = tmp_path / "old-root"
    new_root = tmp_path / "new-root"
    old_root.mkdir(parents=True, exist_ok=True)

    ui_channel.projects_root = old_root.resolve()
    ui_channel._known_project_roots = {old_root.resolve()}
    ws = MagicMock()
    ws.close = AsyncMock()
    ui_channel._clients["PRJ-4224"] = ws
    ui_channel._client_project_dirs["PRJ-4224"] = old_root / "PRJ-4224"

    config = Config()
    monkeypatch.setattr(ui_channel_mod.config_loader, "load_config", lambda _path=None: config)
    monkeypatch.setattr(ui_channel_mod, "save_ui_runtime_update", lambda *_args, **_kwargs: None)

    config_req = MagicMock(spec=web.Request)
    config_req.json = AsyncMock(return_value={"projects_root": str(new_root)})
    config_resp = await ui_channel._handle_config(config_req)

    assert config_resp.status == 200
    ws.close.assert_awaited_once()
    assert ui_channel._clients == {}
    assert ui_channel._client_project_dirs == {}


async def test_handle_config_invalid_json(ui_channel: UiChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.json = AsyncMock(side_effect=json.JSONDecodeError("msg", "", 0))
    resp = await ui_channel._handle_config(req)
    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "invalid JSON"}


async def test_handle_config_updates_projects_root(
    ui_channel: UiChannel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    new_root = tmp_path / "projects"
    new_root.mkdir()
    saved_configs: list[Config] = []
    config = Config()
    config_path = tmp_path / "config_a.json"
    req = MagicMock(spec=web.Request)
    req.json = AsyncMock(return_value={"projects_root": str(new_root)})
    monkeypatch.setattr(ui_channel_mod.config_loader, "get_config_path", lambda: config_path)
    monkeypatch.setattr(ui_channel_mod.config_loader, "load_config", lambda _path=None: config)
    monkeypatch.setattr(
        ui_channel_mod,
        "save_ui_runtime_update",
        lambda cfg, *_args, **_kwargs: saved_configs.append(cfg.model_copy(deep=True)),
    )
    resp = await ui_channel._handle_config(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["projects_root"] == str(new_root.resolve())
    assert body["config_path"] == str(config_path.resolve())
    assert body["persisted"] is True
    assert ui_channel.projects_root == new_root.resolve()
    assert saved_configs[-1].agents.defaults.workspace == str(new_root.resolve())


async def test_handle_config_rejects_non_string_projects_root(ui_channel: UiChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.json = AsyncMock(return_value={"projects_root": 123})
    resp = await ui_channel._handle_config(req)

    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "projects_root must be a string"}


async def test_handle_config_unchanged_without_key(
    ui_channel: UiChannel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui_channel.projects_root = tmp_path
    config = Config()
    config_path = tmp_path / "config_a.json"
    monkeypatch.setattr(ui_channel_mod.config_loader, "get_config_path", lambda: config_path)
    monkeypatch.setattr(ui_channel_mod.config_loader, "load_config", lambda _path=None: config)
    req = MagicMock(spec=web.Request)
    req.json = AsyncMock(return_value={})
    resp = await ui_channel._handle_config(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["projects_root"] == str(tmp_path)
    assert body["persisted"] is False
    assert body["runtime"]["workspace"] == str(tmp_path)


async def test_handle_config_skips_audit_when_projects_root_unchanged(
    ui_channel: UiChannel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    ui_channel.projects_root = root
    audit_calls: list[dict[str, object]] = []
    saved_configs: list[Config] = []
    config = Config()
    config_path = tmp_path / "config_b.json"

    def _capture_audit(**kwargs: object) -> None:
        audit_calls.append(kwargs)

    monkeypatch.setattr(ui_channel, "_audit", _capture_audit)
    monkeypatch.setattr(ui_channel_mod.config_loader, "get_config_path", lambda: config_path)
    monkeypatch.setattr(ui_channel_mod.config_loader, "load_config", lambda _path=None: config)
    monkeypatch.setattr(
        ui_channel_mod,
        "save_ui_runtime_update",
        lambda cfg, *_args, **_kwargs: saved_configs.append(cfg.model_copy(deep=True)),
    )
    req = MagicMock(spec=web.Request)
    req.json = AsyncMock(return_value={"projects_root": str(root)})
    resp = await ui_channel._handle_config(req)

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["projects_root"] == str(root)
    assert body["config_path"] == str(config_path.resolve())
    assert body["persisted"] is True
    assert audit_calls == []
    assert saved_configs[-1].agents.defaults.workspace == str(root)


async def test_handle_get_config_returns_runtime_payload(
    ui_channel: UiChannel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config()
    config.agents.defaults.provider = "openrouter"
    config.agents.defaults.model = "anthropic/claude-sonnet-4-5"
    config.agents.defaults.reasoning_effort = "adaptive"
    config.agents.defaults.max_tool_iterations = 88
    config.providers.openrouter.api_key = "sk-test-key"
    config.providers.openrouter.api_base = "https://openrouter.ai/api/v1"
    config.tools.restrict_to_workspace = True
    config_path = tmp_path / "config_get.json"
    ui_channel.projects_root = (tmp_path / "projects").resolve()

    monkeypatch.setattr(ui_channel_mod.config_loader, "get_config_path", lambda: config_path)
    monkeypatch.setattr(ui_channel_mod.config_loader, "load_config", lambda _path=None: config)
    resp = await ui_channel._handle_get_config(MagicMock(spec=web.Request))

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["projects_root"] == str(ui_channel.projects_root)
    assert body["runtime"]["workspace"] == str(ui_channel.projects_root)
    assert body["runtime"]["workspace_resolved"] == str(ui_channel.projects_root)
    assert body["runtime"]["provider"] == "openrouter"
    assert body["runtime"]["reasoning_effort"] == "adaptive"
    assert body["runtime"]["max_tool_iterations"] == 88
    assert body["runtime"]["restrict_to_workspace"] is True
    assert body["providers"]["openrouter"]["api_key_configured"] is True
    assert body["providers"]["openrouter"]["api_key_preview"] == "sk-t...ey"


async def test_handle_feedback_config_exposes_only_public_state(
    ui_channel: UiChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MIRA_FEISHU_WEBHOOK_URL", "https://open.feishu.cn/webhook/test")
    monkeypatch.setenv("MIRA_FEISHU_WEBHOOK_SECRET", "super-secret")
    monkeypatch.setenv("MIRA_FEISHU_GROUP_INVITE_URL", "https://applink.feishu.cn/client/chat/test")

    resp = await ui_channel._handle_feedback_config(MagicMock(spec=web.Request))

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body == {
        "configured": True,
        "invite_url": "https://applink.feishu.cn/client/chat/test",
    }
    assert "super-secret" not in resp.text
    assert "webhook" not in resp.text


async def test_handle_feedback_requires_backend_config(ui_channel: UiChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.json = AsyncMock(return_value={
        "type": "bug",
        "title": "Cannot save",
        "body": "Save button does nothing.",
    })

    resp = await ui_channel._handle_feedback(req)

    assert resp.status == 503
    assert json.loads(resp.text) == {"error": "not_configured"}


async def test_handle_feedback_submits_to_feishu_relay(
    ui_channel: UiChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MIRA_FEISHU_WEBHOOK_URL", "https://open.feishu.cn/webhook/test")
    monkeypatch.setenv("MIRA_FEISHU_WEBHOOK_SECRET", "super-secret")
    monkeypatch.setenv("MIRA_FEISHU_GROUP_INVITE_URL", "https://applink.feishu.cn/client/chat/test")
    monkeypatch.setenv("MIRA_FEISHU_MENTION_OPEN_ID", "ou_hermes")
    monkeypatch.setenv("MIRA_FEISHU_MENTION_NAME", "Hermes")
    submitted: list[tuple[ui_channel_mod._FeedbackRelayConfig, dict[str, Any]]] = []

    async def _fake_submit(relay: ui_channel_mod._FeedbackRelayConfig, payload: dict[str, Any]) -> None:
        submitted.append((relay, payload))

    monkeypatch.setattr(ui_channel, "_submit_feishu_feedback", _fake_submit)
    req = MagicMock(spec=web.Request)
    req.json = AsyncMock(return_value={
        "id": "fb_test",
        "clientHandle": "MIRA-1234",
        "type": "bug",
        "severity": "critical",
        "title": "Cannot save",
        "body": "Save button does nothing.",
        "contact": {"kind": "email", "value": "user@example.com"},
        "appVersion": "0.4.0",
        "os": "darwin",
        "route": "/",
        "locale": "en",
        "createdAt": "2026-06-02T00:00:00Z",
    })

    resp = await ui_channel._handle_feedback(req)

    assert resp.status == 200
    assert json.loads(resp.text) == {
        "ok": True,
        "channel": "feishu",
        "invite_url": "https://applink.feishu.cn/client/chat/test",
    }
    assert len(submitted) == 1
    relay, payload = submitted[0]
    assert relay.feishu_webhook_url == "https://open.feishu.cn/webhook/test"
    assert relay.feishu_secret == "super-secret"
    assert relay.feishu_mention_open_id == "ou_hermes"
    assert relay.feishu_mention_name == "Hermes"
    assert payload["title"] == "Cannot save"
    assert payload["contact"] == {"kind": "email", "value": "user@example.com"}


def test_resolve_feedback_config_accepts_raw_dict_channel_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "MIRA_FEISHU_WEBHOOK_URL",
        "MIRA_FEISHU_WEBHOOK_SECRET",
        "MIRA_FEISHU_GROUP_INVITE_URL",
        "MIRA_FEISHU_MENTION_OPEN_ID",
        "MIRA_FEISHU_MENTION_NAME",
        "MIRA_FEEDBACK_CONFIG_PATH",
    ):
        monkeypatch.delenv(key, raising=False)

    relay = ui_channel_mod._resolve_feedback_config({
        "feedback": {
            "feishuWebhookUrl": "https://open.feishu.cn/webhook/test",
            "feishuSecret": "super-secret",
            "feishuInviteUrl": "https://applink.feishu.cn/client/chat/test",
            "feishuMentionOpenId": "ou_mirai",
            "feishuMentionName": "MIRAI",
        }
    })

    assert relay.configured is True
    assert relay.feishu_webhook_url == "https://open.feishu.cn/webhook/test"
    assert relay.feishu_secret == "super-secret"
    assert relay.feishu_invite_url == "https://applink.feishu.cn/client/chat/test"
    assert relay.feishu_mention_open_id == "ou_mirai"
    assert relay.feishu_mention_name == "MIRAI"


def test_resolve_feedback_config_accepts_namespace_with_nested_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "MIRA_FEISHU_WEBHOOK_URL",
        "MIRA_FEISHU_WEBHOOK_SECRET",
        "MIRA_FEISHU_GROUP_INVITE_URL",
        "MIRA_FEISHU_MENTION_OPEN_ID",
        "MIRA_FEISHU_MENTION_NAME",
        "MIRA_FEEDBACK_CONFIG_PATH",
    ):
        monkeypatch.delenv(key, raising=False)

    relay = ui_channel_mod._resolve_feedback_config(SimpleNamespace(
        feedback={
            "feishuWebhookUrl": "https://open.feishu.cn/webhook/test",
            "feishuSecret": "super-secret",
        }
    ))

    assert relay.configured is True
    assert relay.feishu_webhook_url == "https://open.feishu.cn/webhook/test"
    assert relay.feishu_secret == "super-secret"


def test_build_feedback_agent_text_includes_full_feedback_payload() -> None:
    text = _build_feedback_agent_text(
        {
            "id": "fb_test",
            "clientHandle": "anon_abcd",
            "type": "feature",
            "severity": None,
            "title": "Add file manager",
            "body": "Show workspace files as a tree.",
            "contact": {"kind": "email", "value": "user@example.com"},
            "appVersion": "0.4.0",
            "os": "darwin",
            "route": "/",
            "locale": "zh",
            "createdAt": "2026-06-02T00:00:00Z",
        },
        mention_open_id="ou_mirai",
        mention_name="MIRAI",
    )

    assert text.startswith('<at user_id="ou_mirai">MIRAI</at> 请处理这条 MIRA feedback。')
    assert "【MIRA Feedback】Feature" in text
    assert "mira_feedback  tag=feature  feedback_id=fb_test" in text
    assert "标题：Add file manager" in text
    assert "内容：\nShow workspace files as a tree." in text
    assert "- 联系：email · user@example.com" in text


def test_build_feedback_agent_text_mentions_mirai_without_open_id() -> None:
    text = _build_feedback_agent_text(
        {
            "id": "fb_test",
            "clientHandle": "anon_abcd",
            "type": "question",
            "severity": None,
            "title": "How to export?",
            "body": "Where is the export button?",
            "contact": None,
            "appVersion": "0.4.0",
            "os": "darwin",
            "route": "/",
            "locale": "zh",
            "createdAt": "2026-06-02T00:00:00Z",
        }
    )

    assert text.startswith("@MIRAI 请处理这条 MIRA feedback。")
    assert "<at user_id=" not in text
    assert "mira_feedback  tag=question  feedback_id=fb_test" in text


async def test_submit_feedback_sends_single_text_message(
    ui_channel: UiChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    relay = ui_channel_mod._FeedbackRelayConfig(
        feishu_webhook_url="https://open.feishu.cn/webhook/test",
        feishu_secret="super-secret",
        feishu_mention_open_id="ou_mirai",
        feishu_mention_name="MIRAI",
    )
    payload = {
        "id": "fb_test",
        "clientHandle": "anon_abcd",
        "type": "feature",
        "severity": None,
        "title": "Add file manager",
        "body": "Show workspace files as a tree.",
        "contact": None,
        "appVersion": "0.4.0",
        "os": "darwin",
        "route": "/",
        "locale": "zh",
        "createdAt": "2026-06-02T00:00:00Z",
    }
    posted: list[dict[str, Any]] = []

    async def _fake_post(
        _relay: ui_channel_mod._FeedbackRelayConfig,
        body: dict[str, Any],
    ) -> None:
        posted.append(body)

    monkeypatch.setattr(ui_channel, "_post_feishu_webhook", _fake_post)

    await ui_channel._submit_feishu_feedback(relay, payload)

    assert len(posted) == 1
    assert posted[0]["msg_type"] == "text"
    assert "card" not in posted[0]
    assert posted[0]["content"]["text"].startswith(
        '<at user_id="ou_mirai">MIRAI</at> 请处理这条 MIRA feedback。'
    )
    assert "mira_feedback" in posted[0]["content"]["text"]
    assert "Show workspace files as a tree." in posted[0]["content"]["text"]


async def test_post_feishu_webhook_retries_ssl_verification_failure(
    ui_channel: UiChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    relay = ui_channel_mod._FeedbackRelayConfig(
        feishu_webhook_url="https://open.feishu.cn/webhook/test",
        feishu_secret="super-secret",
    )
    calls: list[bool | None] = []

    async def _fake_post_once(
        _relay: ui_channel_mod._FeedbackRelayConfig,
        _body: dict[str, Any],
        *,
        ssl: bool | None = None,
    ) -> None:
        calls.append(ssl)
        if ssl is None:
            raise ui_channel_mod.ClientError("CERTIFICATE_VERIFY_FAILED")

    monkeypatch.setattr(ui_channel, "_post_feishu_webhook_once", _fake_post_once)

    await ui_channel._post_feishu_webhook(relay, {"msg_type": "text", "content": {"text": "hello"}})

    assert calls == [None, False]


async def test_post_feishu_webhook_wraps_non_certificate_network_error(
    ui_channel: UiChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    relay = ui_channel_mod._FeedbackRelayConfig(
        feishu_webhook_url="https://open.feishu.cn/webhook/test",
        feishu_secret="super-secret",
    )

    async def _fake_post_once(
        _relay: ui_channel_mod._FeedbackRelayConfig,
        _body: dict[str, Any],
        *,
        ssl: bool | None = None,
    ) -> None:
        raise ui_channel_mod.ClientError("connection refused")

    monkeypatch.setattr(ui_channel, "_post_feishu_webhook_once", _fake_post_once)

    with pytest.raises(RuntimeError, match="feishu webhook request failed: connection refused"):
        await ui_channel._post_feishu_webhook(relay, {"msg_type": "text", "content": {"text": "hello"}})


async def test_handle_config_updates_runtime_fields_and_provider_secrets(
    ui_channel: UiChannel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config()
    config_path = tmp_path / "config_runtime.json"
    saved_configs: list[Config] = []

    monkeypatch.setattr(ui_channel_mod.config_loader, "get_config_path", lambda: config_path)
    monkeypatch.setattr(ui_channel_mod.config_loader, "load_config", lambda _path=None: config)
    monkeypatch.setattr(
        ui_channel_mod,
        "save_ui_runtime_update",
        lambda cfg, *_args, **_kwargs: saved_configs.append(cfg.model_copy(deep=True)),
    )
    req = MagicMock(spec=web.Request)
    req.json = AsyncMock(return_value={
        "runtime": {
            "workspace": str(tmp_path / "bundle-workspace"),
            "provider": "custom",
            "model": "custom/qwen2.5-72b",
            "reasoning_effort": "high",
            "max_tool_iterations": 64,
            "restrict_to_workspace": True,
        },
        "providers": {
            "custom": {
                "api_key": "custom-secret",
                "api_base": "https://llm.example.com/v1",
            }
        },
    })
    resp = await ui_channel._handle_config(req)

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["persisted"] is True
    assert body["runtime"]["provider"] == "custom"
    assert body["runtime"]["model"] == "custom/qwen2.5-72b"
    assert body["runtime"]["reasoning_effort"] == "high"
    assert body["runtime"]["max_tool_iterations"] == 64
    assert body["runtime"]["restrict_to_workspace"] is True
    assert body["providers"]["custom"]["api_key_configured"] is True
    assert body["providers"]["custom"]["api_key_preview"] == "cust...et"
    assert saved_configs[-1].providers.custom.api_key == "custom-secret"
    assert saved_configs[-1].providers.custom.api_base == "https://llm.example.com/v1"


async def test_handle_config_reloads_live_runtime_after_persist(
    ui_channel: UiChannel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config()
    config_path = tmp_path / "config_runtime.json"
    reloads: list[tuple[Config, Path]] = []

    async def _reload_runtime(next_config: Config, projects_root: Path) -> None:
        reloads.append((next_config.model_copy(deep=True), projects_root))

    ui_channel._on_runtime_config_updated = _reload_runtime
    monkeypatch.setattr(ui_channel_mod.config_loader, "get_config_path", lambda: config_path)
    monkeypatch.setattr(ui_channel_mod.config_loader, "load_config", lambda _path=None: config)
    monkeypatch.setattr(ui_channel_mod, "save_ui_runtime_update", lambda *_args, **_kwargs: None)

    req = MagicMock(spec=web.Request)
    req.json = AsyncMock(return_value={
        "runtime": {
            "workspace": str(tmp_path / "bundle-workspace"),
            "provider": "custom",
            "model": "custom/qwen2.5-72b",
            "max_tool_iterations": 64,
        },
        "providers": {
            "custom": {
                "api_base": "https://llm.example.com/v1",
            }
        },
    })

    resp = await ui_channel._handle_config(req)

    assert resp.status == 200
    assert len(reloads) == 1
    reloaded_config, reloaded_root = reloads[0]
    assert reloaded_config.agents.defaults.provider == "custom"
    assert reloaded_config.agents.defaults.model == "custom/qwen2.5-72b"
    assert reloaded_config.providers.custom.api_base == "https://llm.example.com/v1"
    assert reloaded_root == (tmp_path / "bundle-workspace").resolve()


async def test_handle_config_preserves_raw_routing_models_on_runtime_save(
    ui_channel: UiChannel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_root = tmp_path / "old-workspace"
    new_root = tmp_path / "new-workspace"
    ui_channel.projects_root = old_root.resolve()
    config_path = tmp_path / "config_runtime_raw.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "workspace": str(old_root),
                        "provider": "openrouter",
                        "model": ["claude-3-opus", "anthropic/claude-sonnet-4-5"],
                        "routeModel": ["openai/gpt-4.1-mini", "openai/gpt-4.1-nano"],
                        "smallModel": ["deepseek/deepseek-chat", "openai/gpt-4.1-mini"],
                        "mediumModel": "anthropic/claude-sonnet-4-5",
                        "largeModel": "anthropic/claude-opus-4-5",
                    }
                },
                "providers": {
                    "openrouter": {
                        "apiKey": "existing-key",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(ui_channel_mod.config_loader, "get_config_path", lambda: config_path)
    req = MagicMock(spec=web.Request)
    req.json = AsyncMock(return_value={
        "runtime": {
            "workspace": str(new_root),
            "provider": "openrouter",
            "model": "openrouter/claude-3-opus",
            "reasoning_effort": "high",
            "max_tool_iterations": 64,
            "restrict_to_workspace": True,
        },
        "providers": {
            "openrouter": {
                "api_base": "https://openrouter.ai/api/v1",
            }
        },
    })

    resp = await ui_channel._handle_config(req)

    assert resp.status == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    defaults = saved["agents"]["defaults"]
    assert defaults["workspace"] == str(new_root)
    assert defaults["model"] == ["claude-3-opus", "anthropic/claude-sonnet-4-5"]
    assert defaults["routeModel"] == ["openai/gpt-4.1-mini", "openai/gpt-4.1-nano"]
    assert defaults["smallModel"] == ["deepseek/deepseek-chat", "openai/gpt-4.1-mini"]
    assert defaults["mediumModel"] == "anthropic/claude-sonnet-4-5"
    assert defaults["largeModel"] == "anthropic/claude-opus-4-5"
    assert defaults["reasoningEffort"] == "high"
    assert defaults["maxToolIterations"] == 64
    assert saved["tools"]["restrictToWorkspace"] is True
    assert saved["providers"]["openrouter"]["apiKey"] == "existing-key"
    assert saved["providers"]["openrouter"]["apiBase"] == "https://openrouter.ai/api/v1"


async def test_handle_validate_data_path_requires_valid_json(ui_channel: UiChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.json = AsyncMock(side_effect=json.JSONDecodeError("msg", "", 0))
    resp = await ui_channel._handle_validate_data_path(req)
    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "invalid JSON"}


async def test_handle_validate_data_path_success_and_missing(ui_channel: UiChannel) -> None:
    datasets = ui_channel.projects_root / "datasets"
    datasets.mkdir(parents=True)

    req_ok = MagicMock(spec=web.Request)
    req_ok.json = AsyncMock(return_value={"path": "datasets"})
    ok_resp = await ui_channel._handle_validate_data_path(req_ok)
    ok_body = json.loads(ok_resp.text)
    assert ok_resp.status == 200
    assert ok_body["ok"] is True
    assert ok_body["kind"] == "directory"

    req_missing = MagicMock(spec=web.Request)
    req_missing.json = AsyncMock(return_value={"path": "datasets/missing"})
    missing_resp = await ui_channel._handle_validate_data_path(req_missing)
    missing_body = json.loads(missing_resp.text)
    assert missing_resp.status == 200
    assert missing_body["ok"] is False
    assert missing_body["error"] == "path not found"


async def test_handle_validate_data_path_enforces_workspace_boundary(ui_channel: UiChannel, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-datasets"
    outside.mkdir(parents=True, exist_ok=True)
    req = MagicMock(spec=web.Request)
    req.json = AsyncMock(return_value={"path": str(outside)})
    resp = await ui_channel._handle_validate_data_path(req)
    body = json.loads(resp.text)
    assert resp.status == 200
    assert body["ok"] is False
    assert "outside workspace" in body["error"]


async def test_handle_validate_data_path_allows_outside_when_unrestricted(ui_channel: UiChannel, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-open-datasets"
    outside.mkdir(parents=True, exist_ok=True)
    ui_channel.restrict_to_workspace = False

    req = MagicMock(spec=web.Request)
    req.json = AsyncMock(return_value={"path": str(outside)})
    resp = await ui_channel._handle_validate_data_path(req)
    body = json.loads(resp.text)
    assert resp.status == 200
    assert body["ok"] is True
    assert body["kind"] == "directory"


@pytest.mark.asyncio
async def test_organization_folder_and_chat_handlers(ui_channel: UiChannel) -> None:
    create_folder_request = MagicMock(spec=web.Request)
    create_folder_request.json = AsyncMock(return_value={"name": "Lung cancer"})
    folder_response = await ui_channel._handle_create_folder(create_folder_request)
    assert folder_response.status == 201
    folder = json.loads(folder_response.text)

    import_request = MagicMock(spec=web.Request)
    import_request.json = AsyncMock(return_value={
        "chats": [{"id": "chat-1", "title": "Question"}],
    })
    import_response = await ui_channel._handle_import_chats(import_request)
    assert import_response.status == 200

    assign_request = MagicMock(spec=web.Request)
    assign_request.match_info = {"kind": "chat", "item_id": "chat-1"}
    assign_request.json = AsyncMock(return_value={"folder_id": folder["id"]})
    assign_response = await ui_channel._handle_assign_folder(assign_request)
    assert assign_response.status == 200

    snapshot_response = await ui_channel._handle_organization(MagicMock(spec=web.Request))
    snapshot = json.loads(snapshot_response.text)
    assert snapshot["assignments"]["chat"] == {"chat-1": folder["id"]}

    delete_request = MagicMock(spec=web.Request)
    delete_request.match_info = {"folder_id": folder["id"]}
    delete_response = await ui_channel._handle_delete_folder(delete_request)
    assert delete_response.status == 200
    assert ui_channel.organization_store.snapshot()["assignments"]["chat"] == {}


@pytest.mark.asyncio
async def test_assign_folder_rejects_unknown_project_and_folder(ui_channel: UiChannel) -> None:
    unknown_item = MagicMock(spec=web.Request)
    unknown_item.match_info = {"kind": "project", "item_id": "PRJ-missing"}
    unknown_item.json = AsyncMock(return_value={"folder_id": None})
    response = await ui_channel._handle_assign_folder(unknown_item)
    assert response.status == 404

    import_request = MagicMock(spec=web.Request)
    import_request.json = AsyncMock(return_value={"chats": [{"id": "chat-1", "title": "Q"}]})
    await ui_channel._handle_import_chats(import_request)
    unknown_folder = MagicMock(spec=web.Request)
    unknown_folder.match_info = {"kind": "chat", "item_id": "chat-1"}
    unknown_folder.json = AsyncMock(return_value={"folder_id": "folder-missing"})
    response = await ui_channel._handle_assign_folder(unknown_folder)
    assert response.status == 404


async def test_handle_list_projects_only_returns_prj_with_meta(ui_channel: UiChannel) -> None:
    (ui_channel.projects_root / "PRJ-0001").mkdir(parents=True)
    (ui_channel.projects_root / "PRJ-0002").mkdir(parents=True)
    (ui_channel.projects_root / "skills").mkdir(parents=True)
    (ui_channel.projects_root / "random-folder").mkdir(parents=True)

    req = MagicMock(spec=web.Request)
    resp = await ui_channel._handle_list_projects(req)

    assert resp.status == 200
    body = json.loads(resp.text)
    ids = [item["id"] for item in body["projects"]]
    assert ids == ["PRJ-0001", "PRJ-0002"]
    assert [item["display_name"] for item in body["projects"]] == ["PRJ-0001", "PRJ-0002"]
    assert all(item["has_meta"] for item in body["projects"])
    assert all(item["contract_version"] == 1 for item in body["projects"])

    meta_file = ui_channel.projects_root / "PRJ-0001" / ".mira" / "project.json"
    assert meta_file.is_file()
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta["id"] == "PRJ-0001"
    assert meta["display_name"] == "PRJ-0001"
    assert meta["contract_version"] == 1


async def test_handle_create_project_registers_custom_parent(ui_channel: UiChannel, tmp_path: Path) -> None:
    parent = tmp_path / "chosen-parent"
    req = MagicMock(spec=web.Request)
    req.json = AsyncMock(return_value={
        "project_id": "lung-ct-baseline",
        "display_name": "Lung CT Baseline",
        "project_parent_dir": str(parent),
        "run_mode": "manual",
        "agent_profile": "research",
        "contract_version": 2,
    })

    resp = await ui_channel._handle_create_project(req)
    body = json.loads(resp.text)

    assert resp.status == 201
    assert body["id"] == "lung-ct-baseline"
    assert body["display_name"] == "Lung CT Baseline"
    assert body["project_dir"] == str((parent / "lung-ct-baseline").resolve())
    assert body["run_mode"] == "manual"
    assert body["agent_profile"] == "research"
    assert body["contract_version"] == 2

    workspace_file = ui_channel._project_workspace_path
    registry = json.loads(workspace_file.read_text(encoding="utf-8"))
    assert registry["projects"][0]["id"] == "lung-ct-baseline"

    list_resp = await ui_channel._handle_list_projects(MagicMock(spec=web.Request))
    list_body = json.loads(list_resp.text)
    assert [item["id"] for item in list_body["projects"]] == ["lung-ct-baseline"]


async def test_handle_create_project_managed_mode_ignores_client_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = MagicMock(spec=UiChannelConfig)
    config.project_storage = "managed"
    config.managed_project_root = str(tmp_path / "managed")
    bus = MagicMock(spec=MessageBus)
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr(
        ui_channel_mod,
        "get_runtime_subdir",
        lambda name: (runtime_root / name).mkdir(parents=True, exist_ok=True) or (runtime_root / name),
    )
    with patch.object(BaseChannel, "__init__", _minimal_base_init):
        with patch.object(ui_channel_mod, "_load_ui_instructions", return_value=""):
            ch = UiChannel(config, bus, workspace=tmp_path / "local")

    req = MagicMock(spec=web.Request)
    req.json = AsyncMock(return_value={
        "project_id": "cloud-project",
        "display_name": "Cloud Project",
        "project_parent_dir": str(tmp_path / "client-choice"),
    })

    resp = await ch._handle_create_project(req)
    body = json.loads(resp.text)

    assert resp.status == 201
    assert body["project_dir"] == str((tmp_path / "managed" / "cloud-project").resolve())
    assert ch._project_location_payload()["custom_dir_allowed"] is False


async def test_handle_project_meta_updates_display_name(ui_channel: UiChannel) -> None:
    project_dir = ui_channel.projects_root / "PRJ-0001"
    project_dir.mkdir(parents=True)

    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0001"}
    req.json = AsyncMock(return_value={"display_name": "Lung CT baseline"})
    resp = await ui_channel._handle_project_meta(req)

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["display_name"] == "Lung CT baseline"
    assert body["run_mode"] == "auto"
    assert body["agent_profile"] == "research"
    assert body["contract_version"] == 1

    meta_file = project_dir / ".mira" / "project.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta["display_name"] == "Lung CT baseline"
    assert meta["run_mode"] == "auto"
    assert meta["agent_profile"] == "research"
    assert meta["contract_version"] == 1


async def test_handle_project_meta_updates_run_mode_and_profile(
    ui_channel: UiChannel,
) -> None:
    project_dir = ui_channel.projects_root / "PRJ-0002"
    project_dir.mkdir(parents=True)

    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0002"}
    req.json = AsyncMock(return_value={
        "run_mode": "manual",
        "agent_profile": "research",
    })
    resp = await ui_channel._handle_project_meta(req)

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["display_name"] == "PRJ-0002"
    assert body["run_mode"] == "manual"
    assert body["agent_profile"] == "research"
    assert body["contract_version"] == 1

    meta_file = project_dir / ".mira" / "project.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta["run_mode"] == "manual"
    assert meta["agent_profile"] == "research"
    assert meta["contract_version"] == 1


async def test_handle_project_meta_updates_contract_version(
    ui_channel: UiChannel,
) -> None:
    project_dir = ui_channel.projects_root / "PRJ-0003"
    project_dir.mkdir(parents=True)

    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0003"}
    req.json = AsyncMock(return_value={"contract_version": 2})
    resp = await ui_channel._handle_project_meta(req)

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["contract_version"] == 2

    meta_file = project_dir / ".mira" / "project.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta["contract_version"] == 2


async def test_handle_project_meta_updates_automation_policy(
    ui_channel: UiChannel,
) -> None:
    project_dir = ui_channel.projects_root / "PRJ-0008"
    project_dir.mkdir(parents=True)

    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0008"}
    req.json = AsyncMock(return_value={
        "automation_policy": {
            "logic": "OR",
            "goals": [{"metric": "Dice", "operator": ">", "value": 0.8}],
            "maxExperiments": 12,
            "maxTokens": 200000,
        }
    })
    resp = await ui_channel._handle_project_meta(req)

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["automation_policy"]["logic"] == "OR"
    assert body["automation_policy"]["maxExperiments"] == 12

    meta_file = project_dir / ".mira" / "project.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta["automation_policy"]["goals"][0]["metric"] == "Dice"


async def test_cors_allows_patch_method(ui_channel: UiChannel) -> None:
    ui_channel.config.cors_origins = ["*"]
    req = MagicMock(spec=web.Request)
    req.method = "OPTIONS"
    req.headers = {"Origin": "http://localhost:5173"}

    resp = await ui_channel._cors_middleware(req, AsyncMock())
    assert resp.status == 204
    assert resp.headers["Access-Control-Allow-Methods"] == "GET, POST, PATCH, DELETE, OPTIONS"
    assert resp.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"


async def test_handle_upload_project_files_invalid_multipart(ui_channel: UiChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0001"}
    req.query = {}
    req.multipart = AsyncMock(side_effect=RuntimeError("bad form"))

    resp = await ui_channel._handle_upload_project_files(req)
    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "expected multipart/form-data"}


async def test_handle_upload_project_files_missing_files(ui_channel: UiChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0001"}
    req.query = {}
    req.multipart = AsyncMock(return_value=_FakeMultipart([
        _FakePart(name="metadata", filename="ignored.txt", chunks=[b"abc"]),
    ]))

    resp = await ui_channel._handle_upload_project_files(req)
    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "no files uploaded"}


async def test_handle_upload_project_files_writes_data_files(ui_channel: UiChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0001"}
    req.query = {}
    req.multipart = AsyncMock(return_value=_FakeMultipart([
        _FakePart(name="files", filename="sample.csv", chunks=[b"a,", b"b\n"]),
        _FakePart(name="files", filename="sample.csv", chunks=[b"c,d\n"]),
    ]))

    resp = await ui_channel._handle_upload_project_files(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["session_id"] == "PRJ-0001"
    assert body["target"] == "data"
    assert body["uploaded"] == [
        {"name": "sample.csv", "path": "data/sample.csv", "size": 4},
        {"name": "sample_1.csv", "path": "data/sample_1.csv", "size": 4},
    ]
    assert body["extracted"] == []

    data_dir = ui_channel.projects_root / "PRJ-0001" / "data"
    assert (data_dir / "sample.csv").read_bytes() == b"a,b\n"
    assert (data_dir / "sample_1.csv").read_bytes() == b"c,d\n"


async def test_handle_upload_project_files_preserves_data_directory_paths(ui_channel: UiChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0002"}
    req.query = {}
    req.multipart = AsyncMock(return_value=_FakeMultipart([
        _FakePart(name="files", filename="dataset/tables/a.csv", chunks=[b"a,b\n"]),
        _FakePart(name="files", filename="dataset/tables/a.csv", chunks=[b"c,d\n"]),
        _FakePart(name="files", filename="dataset/notes/readme.txt", chunks=[b"ok"]),
    ]))

    resp = await ui_channel._handle_upload_project_files(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["uploaded"] == [
        {"name": "a.csv", "path": "data/dataset/tables/a.csv", "size": 4},
        {"name": "a_1.csv", "path": "data/dataset/tables/a_1.csv", "size": 4},
        {"name": "readme.txt", "path": "data/dataset/notes/readme.txt", "size": 2},
    ]

    data_dir = ui_channel.projects_root / "PRJ-0002" / "data"
    assert (data_dir / "dataset" / "tables" / "a.csv").read_bytes() == b"a,b\n"
    assert (data_dir / "dataset" / "tables" / "a_1.csv").read_bytes() == b"c,d\n"
    assert (data_dir / "dataset" / "notes" / "readme.txt").read_bytes() == b"ok"


async def test_handle_upload_project_files_rejects_data_path_traversal(ui_channel: UiChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0003"}
    req.query = {}
    req.multipart = AsyncMock(return_value=_FakeMultipart([
        _FakePart(name="files", filename="../escape.csv", chunks=[b"bad"]),
    ]))

    resp = await ui_channel._handle_upload_project_files(req)
    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "unsafe upload path: ../escape.csv"}
    assert not (ui_channel.projects_root / "escape.csv").exists()


async def test_handle_upload_project_files_rejects_absolute_data_paths(ui_channel: UiChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0004"}
    req.query = {}
    req.multipart = AsyncMock(return_value=_FakeMultipart([
        _FakePart(name="files", filename="/tmp/escape.csv", chunks=[b"bad"]),
    ]))

    resp = await ui_channel._handle_upload_project_files(req)
    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "unsafe upload path: /tmp/escape.csv"}
    assert not (ui_channel.projects_root / "PRJ-0004" / "data" / "tmp").exists()


async def test_handle_upload_project_files_references_extracts_zip(
    ui_channel: UiChannel,
) -> None:
    zip_buf = BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("papers/paper_a.pdf", b"%PDF-1.4")
        zf.writestr("notes/summary.txt", b"ok")

    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0002"}
    req.query = {"target": "references"}
    req.multipart = AsyncMock(return_value=_FakeMultipart([
        _FakePart(name="files", filename="seed.pdf", chunks=[b"%PDF-1.7"]),
        _FakePart(name="files", filename="bundle.zip", chunks=[zip_buf.getvalue()]),
    ]))

    resp = await ui_channel._handle_upload_project_files(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["session_id"] == "PRJ-0002"
    assert body["target"] == "references"
    assert body["uploaded"] == [
        {"name": "seed.pdf", "path": "references/seed.pdf", "size": 8},
        {"name": "bundle.zip", "path": "references/bundle.zip", "size": len(zip_buf.getvalue())},
    ]
    extracted_paths = {item["path"] for item in body["extracted"]}
    assert extracted_paths == {
        "references/bundle/papers/paper_a.pdf",
        "references/bundle/notes/summary.txt",
    }

    refs_dir = ui_channel.projects_root / "PRJ-0002" / "references"
    assert (refs_dir / "seed.pdf").read_bytes() == b"%PDF-1.7"
    assert (refs_dir / "bundle" / "papers" / "paper_a.pdf").read_bytes() == b"%PDF-1.4"
    assert (refs_dir / "bundle" / "notes" / "summary.txt").read_bytes() == b"ok"


async def test_handle_upload_project_files_references_rejects_non_pdf_zip(
    ui_channel: UiChannel,
) -> None:
    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0003"}
    req.query = {"target": "references"}
    req.multipart = AsyncMock(return_value=_FakeMultipart([
        _FakePart(name="files", filename="notes.txt", chunks=[b"hello"]),
    ]))

    resp = await ui_channel._handle_upload_project_files(req)
    assert resp.status == 400
    assert json.loads(resp.text) == {
        "error": "references uploads only support .pdf and .zip files"
    }


async def test_handle_upload_project_files_references_rejects_zip_slip(
    ui_channel: UiChannel,
) -> None:
    zip_buf = BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("../escape.txt", b"bad")

    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0004"}
    req.query = {"target": "references"}
    req.multipart = AsyncMock(return_value=_FakeMultipart([
        _FakePart(name="files", filename="unsafe.zip", chunks=[zip_buf.getvalue()]),
    ]))

    resp = await ui_channel._handle_upload_project_files(req)
    assert resp.status == 400
    assert "unsafe zip entry" in json.loads(resp.text)["error"]
    assert not (ui_channel.projects_root / "escape.txt").exists()


async def test_handle_project_artifact_serves_file(ui_channel: UiChannel) -> None:
    project_dir = ui_channel.projects_root / "PRJ-0001"
    artifact = project_dir / "experiments" / "exp005" / "roc_pr_curves.png"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"png")

    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0001"}
    req.query = {"path": "experiments/exp005/roc_pr_curves.png"}

    resp = await ui_channel._handle_project_artifact(req)
    assert isinstance(resp, web.FileResponse)
    assert resp.status == 200
    assert Path(resp._path) == artifact


async def test_handle_project_artifact_blocks_traversal(ui_channel: UiChannel, tmp_path: Path) -> None:
    project_dir = ui_channel.projects_root / "PRJ-0001"
    project_dir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")

    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-0001"}
    req.query = {"path": "../outside.txt"}

    resp = await ui_channel._handle_project_artifact(req)
    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "invalid artifact path"}


async def test_skill_plugin_install_from_directory_and_list(ui_channel: UiChannel, tmp_path: Path) -> None:
    src = _create_plugin_source(tmp_path)
    install_req = MagicMock(spec=web.Request)
    install_req.match_info = {"session_id": "PRJ-0001"}
    install_req.headers = {"Content-Type": "application/json"}
    install_req.json = AsyncMock(return_value={"path": str(src)})

    install_resp = await ui_channel._handle_skill_plugins_install(install_req)
    assert install_resp.status == 200
    body = json.loads(install_resp.text)
    assert body["installed"]["id"] == "plugin-pack"

    list_req = MagicMock(spec=web.Request)
    list_req.match_info = {"session_id": "PRJ-0001"}
    list_resp = await ui_channel._handle_skill_plugins_list(list_req)
    assert list_resp.status == 200
    list_body = json.loads(list_resp.text)
    assert {p["id"] for p in list_body["plugins"]} >= {"builtin-skills", "plugin-pack"}


async def test_skill_plugin_install_from_zip(ui_channel: UiChannel, tmp_path: Path) -> None:
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

    resp = await ui_channel._handle_skill_plugins_install(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["installed"]["id"] == "zip-pack"


async def test_skill_plugin_install_from_zip_without_manifest(ui_channel: UiChannel, tmp_path: Path) -> None:
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

    resp = await ui_channel._handle_skill_plugins_install(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["installed"]["id"] == "no-manifest-pack"


async def test_skill_plugin_toggle_and_uninstall(ui_channel: UiChannel, tmp_path: Path) -> None:
    src = _create_plugin_source(tmp_path)
    install_req = MagicMock(spec=web.Request)
    install_req.match_info = {"session_id": "PRJ-0001"}
    install_req.headers = {"Content-Type": "application/json"}
    install_req.json = AsyncMock(return_value={"path": str(src)})
    await ui_channel._handle_skill_plugins_install(install_req)

    toggle_req = MagicMock(spec=web.Request)
    toggle_req.match_info = {"session_id": "PRJ-0001"}
    toggle_req.json = AsyncMock(return_value={
        "scope": "global",
        "target_type": "skill",
        "plugin_id": "plugin-pack",
        "target_id": "writer",
        "enabled": False,
    })
    toggle_resp = await ui_channel._handle_skill_plugins_state(toggle_req)
    assert toggle_resp.status == 200
    toggle_body = json.loads(toggle_resp.text)
    plugin_pack = next(item for item in toggle_body["plugins"] if item["id"] == "plugin-pack")
    writer = next(item for item in plugin_pack["skills"] if item["id"] == "writer")
    assert writer["enabled"]["effective"] is False

    remove_req = MagicMock(spec=web.Request)
    remove_req.match_info = {"session_id": "PRJ-0001", "plugin_id": "plugin-pack"}
    remove_resp = await ui_channel._handle_skill_plugins_uninstall(remove_req)
    assert remove_resp.status == 200
    remove_body = json.loads(remove_resp.text)
    assert [item["id"] for item in remove_body["plugins"]] == ["builtin-skills"]


async def test_send_delivers_json_to_open_socket(ui_channel: UiChannel) -> None:
    ws = MagicMock()
    ws.closed = False
    ws.send_json = AsyncMock()
    ui_channel._clients["sid-1"] = ws
    msg = OutboundMessage(
        channel="ui",
        chat_id="sid-1",
        content="hello",
        media=["u1"],
        metadata={"k": "v"},
    )
    await ui_channel.send(msg)
    ws.send_json.assert_awaited_once()
    payload = ws.send_json.await_args.args[0]
    assert payload == {
        "type": "response",
        "session_id": "sid-1",
        "content": "hello",
        "media": ["u1"],
        "metadata": {"k": "v"},
    }


async def test_send_progress_type(ui_channel: UiChannel) -> None:
    ws = MagicMock()
    ws.closed = False
    ws.send_json = AsyncMock()
    ui_channel._clients["x"] = ws
    msg = OutboundMessage(
        channel="ui",
        chat_id="x",
        content="…",
        metadata={"_progress": True},
    )
    await ui_channel.send(msg)
    assert ws.send_json.await_args.args[0]["type"] == "progress"


async def test_send_stop_ack_is_control_only(ui_channel: UiChannel) -> None:
    session_id = "sid-stop"
    project_dir = ui_channel.projects_root / session_id
    project_dir.mkdir(parents=True)
    ws = MagicMock()
    ws.closed = False
    ws.send_json = AsyncMock()
    ui_channel._clients[session_id] = ws
    ui_channel._stream_buffers[session_id] = "partial"

    await ui_channel.send(OutboundMessage(
        channel="ui",
        chat_id=session_id,
        content="Stopped 1 task(s).",
        metadata={"_stop_ack": True, "request_id": "stop-1"},
    ))

    assert ws.send_json.await_args.args[0]["type"] == "stop_ack"
    assert session_id not in ui_channel._stream_buffers
    assert SessionManager(project_dir).get_ui_history(f"ui:{session_id}") == []


async def test_send_activity_ping_does_not_persist_history(ui_channel: UiChannel) -> None:
    session_id = "sid-activity"
    project_dir = ui_channel.projects_root / session_id
    project_dir.mkdir(parents=True)

    ws = MagicMock()
    ws.closed = False
    ws.send_json = AsyncMock()
    ui_channel._clients[session_id] = ws
    msg = OutboundMessage(
        channel="ui",
        chat_id=session_id,
        content="Mira is working...",
        metadata={"_progress": True, "_activity_ping": True},
    )
    await ui_channel.send(msg)
    assert ws.send_json.await_args.args[0]["type"] == "progress"
    assert SessionManager(project_dir).get_ui_history(f"ui:{session_id}") == []


async def test_send_writes_project_audit_entry(ui_channel: UiChannel) -> None:
    session_id = "sid-log"
    project_dir = ui_channel.projects_root / session_id
    project_dir.mkdir(parents=True)

    ws = MagicMock()
    ws.closed = False
    ws.send_json = AsyncMock()
    ui_channel._clients[session_id] = ws

    msg = OutboundMessage(
        channel="ui",
        chat_id=session_id,
        content="running exp",
        metadata={"_progress": True, "_tool_hint": True},
    )
    await ui_channel.send(msg)

    project_log = project_dir / ".mira" / "logs" / "actions.jsonl"
    assert project_log.is_file()
    entry = json.loads(project_log.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert entry["source"] == "agent"
    assert entry["action"] == "ws_outbound_sent"
    assert entry["details"]["type"] == "progress"
    assert entry["details"]["tool_hint"] is True


async def test_send_audit_only_skill_event_writes_project_log(ui_channel: UiChannel) -> None:
    session_id = "sid-skill-log"
    project_dir = ui_channel.projects_root / session_id
    project_dir.mkdir(parents=True)

    msg = OutboundMessage(
        channel="ui",
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
    await ui_channel.send(msg)

    project_log = project_dir / ".mira" / "logs" / "actions.jsonl"
    assert project_log.is_file()
    entry = json.loads(project_log.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert entry["source"] == "agent"
    assert entry["action"] == "skill_invoked"
    assert entry["details"]["tool"] == "read_file"
    assert entry["details"]["skill_name"] == "scientific-method"


async def test_send_no_client_noop(ui_channel: UiChannel) -> None:
    msg = OutboundMessage(channel="ui", chat_id="missing", content="x")
    await ui_channel.send(msg)


async def test_send_closed_socket_noop(ui_channel: UiChannel) -> None:
    ws = MagicMock()
    ws.closed = True
    ws.send_json = AsyncMock()
    ui_channel._clients["gone"] = ws
    await ui_channel.send(OutboundMessage(channel="ui", chat_id="gone", content="x"))
    ws.send_json.assert_not_called()


async def test_send_send_json_failure_swallowed(ui_channel: UiChannel) -> None:
    ws = MagicMock()
    ws.closed = False
    ws.send_json = AsyncMock(side_effect=RuntimeError("broken"))
    ui_channel._clients["err"] = ws
    await ui_channel.send(OutboundMessage(channel="ui", chat_id="err", content="x"))


def test_web_helpers_cover_normalization_and_formatting() -> None:
    assert _normalize_run_mode(" AUTO ") == "auto"
    assert _normalize_run_mode("unknown") == "manual"
    assert _normalize_loop_mode(" NORMAL ") == "normal"
    assert _normalize_loop_mode("unknown") == "project"
    assert _normalize_agent_profile(" ENGINEER ") == "engineer"
    assert _normalize_agent_profile("bad") == "research"
    assert _normalize_contract_version(2) == 2
    assert _normalize_contract_version(None) == 1
    assert _safe_upload_name("../x.txt") == "x.txt"
    assert _stringify_history_content([{"type": "text", "text": "A"}, {"type": "image_url"}]) == "A\n[image]"
    assert _stringify_history_content({"k": 1}) == '{"k": 1}'
    assert _format_tool_call({"function": {"name": "read_file", "arguments": "{\"path\":\"a\"}"}}) == 'read_file({"path":"a"})'


def test_reconcile_plan_data_without_experiments_returns_false(ui_channel: UiChannel, tmp_path: Path) -> None:
    payload = {"title": "demo"}
    assert ui_channel._reconcile_plan_data(tmp_path, payload) is False
    assert payload == {"title": "demo"}


def test_load_plan_data_errors_and_reconcile_write_warning(
    ui_channel: UiChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = ui_channel.projects_root / "PRJ-3001"
    project.mkdir(parents=True)
    plan = project / PLAN_FILENAME
    plan.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="Unexpected non-object JSON"):
        ui_channel._load_plan_data("PRJ-3001")

    plan.write_text(json.dumps({"experiments": []}), encoding="utf-8")
    monkeypatch.setattr(ui_channel, "_reconcile_plan_data", lambda *a, **k: True)
    monkeypatch.setattr(Path, "write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    data = ui_channel._load_plan_data("PRJ-3001")
    assert data == {"experiments": []}


class _FakeWsMessage:
    def __init__(self, msg_type, data: str):
        self.type = msg_type
        self.data = data


class _FakeWs:
    def __init__(self, messages: list[_FakeWsMessage]) -> None:
        self._messages = list(messages)
        self.closed = False
        self.sent = []

    async def prepare(self, request) -> None:
        return None

    def __aiter__(self):
        async def _gen():
            for item in self._messages:
                yield item
        return _gen()

    async def send_json(self, payload) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True


async def test_ws_handler_invalid_json_and_missing_session_id(
    ui_channel: UiChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _FakeWs(
        [
            _FakeWsMessage(web.WSMsgType.TEXT, "{"),
            _FakeWsMessage(web.WSMsgType.TEXT, json.dumps({"type": "message", "content": "x"})),
        ]
    )
    monkeypatch.setattr(ui_channel_mod.web, "WebSocketResponse", lambda: ws)
    req = MagicMock(spec=web.Request)
    await ui_channel._ws_handler(req)
    assert ws.sent[0]["content"] == "Invalid JSON"
    assert ws.sent[1]["content"] == "session_id required"


async def test_ws_handler_message_and_set_mode_dispatch(
    ui_channel: UiChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages = [
        _FakeWsMessage(
            web.WSMsgType.TEXT,
            json.dumps(
                {
                    "type": "message",
                    "session_id": "PRJ-4001",
                    "user_id": "u1",
                    "mode": "AUTO",
                    "agent_profile": "engineer",
                    "contract_version": 2,
                    "automation_policy": {
                        "logic": "AND",
                        "goals": [{"metric": "Dice", "operator": ">", "value": 0.8}],
                        "maxExperiments": 8,
                    },
                    "content": "hello",
                    "media": ["a.png"],
                    "turn_id": "turn-1",
                    "selected_skill_ids": ["mrstation", ""],
                }
            ),
        ),
        _FakeWsMessage(
            web.WSMsgType.TEXT,
            json.dumps(
                {
                    "type": "set_mode",
                    "session_id": "PRJ-4001",
                    "user_id": "u1",
                    "mode": "manual",
                }
            ),
        ),
    ]
    ws = _FakeWs(messages)
    monkeypatch.setattr(ui_channel_mod.web, "WebSocketResponse", lambda: ws)
    ui_channel._ui_instructions = "UI instruction"
    handled = []

    async def _handle_message(**kwargs):
        handled.append(kwargs)

    monkeypatch.setattr(ui_channel, "_handle_message", _handle_message)
    monkeypatch.setattr(ui_channel, "_load_plan_data", lambda *_a, **_k: None)
    req = MagicMock(spec=web.Request)
    await ui_channel._ws_handler(req)

    assert len(handled) == 2
    assert handled[0]["metadata"]["run_mode"] == "auto"
    assert handled[0]["metadata"]["agent_profile"] == "engineer"
    assert handled[0]["metadata"]["contract_version"] == 2
    assert handled[0]["metadata"]["turn_id"] == "turn-1"
    assert handled[0]["metadata"]["selected_skill_ids"] == ["mrstation"]
    assert handled[0]["metadata"]["automation_policy"]["maxExperiments"] == 8
    assert "_ui_system_instructions" in handled[0]["metadata"]
    assert handled[1]["metadata"]["_control"] == "set_mode"

    meta_file = ui_channel.projects_root / "PRJ-4001" / ".mira" / "project.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta["contract_version"] == 2
    assert meta["automation_policy"]["goals"][0]["metric"] == "Dice"


async def test_ws_handler_stop_dispatches_control_message(
    ui_channel: UiChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _FakeWs([
        _FakeWsMessage(
            web.WSMsgType.TEXT,
            json.dumps({
                "type": "stop",
                "session_id": "chat-1",
                "loop_mode": "normal",
                "turn_id": "turn-1",
                "request_id": "stop-1",
            }),
        )
    ])
    monkeypatch.setattr(ui_channel_mod.web, "WebSocketResponse", lambda: ws)
    handled = []

    async def _handle_message(**kwargs):
        handled.append(kwargs)

    monkeypatch.setattr(ui_channel, "_handle_message", _handle_message)
    await ui_channel._ws_handler(MagicMock(spec=web.Request))

    assert len(handled) == 1
    assert handled[0]["content"] == "/stop"
    assert handled[0]["session_key"] == "ui:chat-1"
    assert handled[0]["metadata"]["_stop_request_id"] == "stop-1"
    assert handled[0]["metadata"]["turn_id"] == "turn-1"


async def test_ws_handler_normal_message_skips_project_runtime_state(
    ui_channel: UiChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "__normal__"
    ws = _FakeWs([
        _FakeWsMessage(
            web.WSMsgType.TEXT,
            json.dumps(
                {
                    "type": "message",
                    "session_id": session_id,
                    "user_id": "u1",
                    "loop_mode": "normal",
                    "mode": "auto",
                    "agent_profile": "research",
                    "content": "general question",
                    "media": [],
                }
            ),
        ),
    ])
    monkeypatch.setattr(ui_channel_mod.web, "WebSocketResponse", lambda: ws)
    ui_channel._ui_instructions = "UI instruction"
    captured: dict[str, Any] = {}

    async def _handle_message(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(ui_channel, "_handle_message", _handle_message)
    req = MagicMock(spec=web.Request)
    await ui_channel._ws_handler(req)

    assert captured["chat_id"] == session_id
    assert captured["metadata"]["loop_mode"] == "normal"
    assert "project_dir" not in captured["metadata"]
    assert "_ui_system_instructions" not in captured["metadata"]
    assert not (ui_channel.projects_root / session_id).exists()


async def test_ws_handler_injects_guard_notice_on_id_reassignment(
    ui_channel: UiChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "PRJ-4012"
    project_dir = ui_channel.projects_root / session_id
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / PLAN_FILENAME).write_text(
        json.dumps(
            {
                "title": "demo",
                "status": "in_progress",
                "experiments": [
                    {"id": "Exp003", "status": "pending"},
                    {"id": "Exp003", "status": "pending"},
                ],
            }
        ),
        encoding="utf-8",
    )

    ws = _FakeWs([
        _FakeWsMessage(
            web.WSMsgType.TEXT,
            json.dumps(
                {
                    "type": "message",
                    "session_id": session_id,
                    "user_id": "u1",
                    "mode": "auto",
                    "agent_profile": "research",
                    "content": "check latest exp ids",
                    "media": [],
                }
            ),
        ),
    ])
    monkeypatch.setattr(ui_channel_mod.web, "WebSocketResponse", lambda: ws)
    captured: dict[str, Any] = {}

    async def _handle_message(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(ui_channel, "_handle_message", _handle_message)
    req = MagicMock(spec=web.Request)
    await ui_channel._ws_handler(req)

    notice = captured.get("metadata", {}).get("_task_plan_guard_notice")
    assert isinstance(notice, str)
    assert "Exp003 -> Exp004" in notice

    repaired = json.loads((project_dir / PLAN_FILENAME).read_text(encoding="utf-8"))
    ids = [item.get("id") for item in repaired.get("experiments", [])]
    assert ids == ["Exp003", "Exp004"]


async def test_ws_handler_bind_registers_active_client(
    ui_channel: UiChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    (ui_channel.projects_root / "PRJ-4011").mkdir(parents=True, exist_ok=True)
    ws = _FakeWs([
        _FakeWsMessage(
            web.WSMsgType.TEXT,
            json.dumps(
                {
                    "type": "bind",
                    "session_id": "PRJ-4011",
                    "user_id": "u1",
                }
            ),
        ),
    ])
    monkeypatch.setattr(ui_channel_mod.web, "WebSocketResponse", lambda: ws)
    req = MagicMock(spec=web.Request)
    await ui_channel._ws_handler(req)

    assert "PRJ-4011" not in ui_channel._clients
    # Connection closes after handler loop exits; verify bind was accepted via audit entry.
    audit_log = ui_channel.projects_root / "PRJ-4011" / ".mira" / "logs" / "actions.jsonl"
    assert audit_log.is_file()
    lines = [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(item.get("action") == "ws_bind_received" for item in lines)


async def test_ws_handler_persists_ui_chat_user_entry(
    ui_channel: UiChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "PRJ-4010"
    (ui_channel.projects_root / session_id).mkdir(parents=True)
    ws = _FakeWs([
        _FakeWsMessage(
            web.WSMsgType.TEXT,
            json.dumps({
                "type": "message",
                "session_id": session_id,
                "user_id": "u1",
                "content": "persist me",
                "media": [],
            }),
        ),
    ])
    monkeypatch.setattr(ui_channel_mod.web, "WebSocketResponse", lambda: ws)

    async def _handle_message(**kwargs):
        return None

    monkeypatch.setattr(ui_channel, "_handle_message", _handle_message)
    req = MagicMock(spec=web.Request)
    await ui_channel._ws_handler(req)

    manager = SessionManager(ui_channel.projects_root / session_id)
    session = manager.get_or_create(f"ui:{session_id}")
    assert any(event.get("role") == "user" and event.get("content") == "persist me" for event in session.ui_events)


async def test_handle_status_and_sessions_endpoints(ui_channel: UiChannel) -> None:
    ws = MagicMock()
    ws.closed = False
    ui_channel._clients = {"PRJ-5001": ws}
    ui_channel.bind_host = "127.0.0.1"
    ui_channel.bind_port = 18790
    req = MagicMock(spec=web.Request)

    status = await ui_channel._handle_status(req)
    status_body = json.loads(status.text)
    assert status_body["channel"] == "ui"
    assert status_body["connected_clients"] == 1

    sessions = await ui_channel._handle_sessions(req)
    sessions_body = json.loads(sessions.text)
    assert sessions_body == {"sessions": [{"session_id": "PRJ-5001", "connected": True}]}


async def test_handle_history_requires_session_id(ui_channel: UiChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": ""}
    resp = await ui_channel._handle_history(req)
    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "session_id required"}


async def test_handle_delete_project_paths(ui_channel: UiChannel, monkeypatch: pytest.MonkeyPatch) -> None:
    req_missing = MagicMock(spec=web.Request)
    req_missing.query = {}
    missing_id = await ui_channel._handle_delete_project(req_missing)
    assert missing_id.status == 400

    req_not_found = MagicMock(spec=web.Request)
    req_not_found.query = {"session_id": "PRJ-NOPE"}
    not_found = await ui_channel._handle_delete_project(req_not_found)
    assert json.loads(not_found.text)["deleted"] is False

    project = ui_channel.projects_root / "PRJ-DEL"
    project.mkdir(parents=True)
    req_ok = MagicMock(spec=web.Request)
    req_ok.query = {"session_id": "PRJ-DEL"}
    ok = await ui_channel._handle_delete_project(req_ok)
    assert json.loads(ok.text) == {"deleted": True, "removed": True}

    project2 = ui_channel.projects_root / "PRJ-ERR"
    project2.mkdir(parents=True)
    req_err = MagicMock(spec=web.Request)
    req_err.query = {"session_id": "PRJ-ERR"}

    def _boom(_):
        raise OSError("cannot delete")

    monkeypatch.setattr(ui_channel_mod.shutil, "rmtree", _boom)
    err = await ui_channel._handle_delete_project(req_err)
    assert err.status == 500
    assert "cannot delete" in json.loads(err.text)["error"]


async def test_handle_remove_project_keeps_files_hidden_from_list(ui_channel: UiChannel) -> None:
    project = ui_channel.projects_root / "PRJ-KEEP"
    project.mkdir(parents=True)

    req = MagicMock(spec=web.Request)
    req.match_info = {"session_id": "PRJ-KEEP"}
    resp = await ui_channel._handle_remove_project(req)

    assert resp.status == 200
    assert json.loads(resp.text) == {"deleted": False, "removed": True}
    assert project.is_dir()

    list_resp = await ui_channel._handle_list_projects(MagicMock(spec=web.Request))
    list_body = json.loads(list_resp.text)
    assert [item["id"] for item in list_body["projects"]] == []


def _model_cache(provider: str = "deepseek") -> "Any":
    from mira_engine.providers.model_fetch import ModelCache, ModelInfo

    return ModelCache(
        provider=provider,
        api_base="https://api.deepseek.com",
        fetched_at="2026-05-13T12:00:00Z",
        models=[
            ModelInfo(id="deepseek-chat", config_model="deepseek/deepseek-chat"),
            ModelInfo(id="deepseek-reasoner", config_model="deepseek/deepseek-reasoner"),
        ],
    )


async def test_handle_provider_models_fetches_and_returns_ids(
    ui_channel: UiChannel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config()
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(ui_channel_mod.config_loader, "get_config_path", lambda: config_path)
    monkeypatch.setattr(ui_channel_mod.config_loader, "load_config", lambda _path=None: config)
    monkeypatch.setattr(ui_channel_mod, "read_model_cache", lambda *_a, **_k: None)

    async def fake_fetch(cfg, provider, cfg_path):
        return _model_cache(provider), tmp_path / "models" / "deepseek.json"

    monkeypatch.setattr(ui_channel_mod, "fetch_models_to_cache", fake_fetch)

    req = MagicMock(spec=web.Request)
    req.match_info = {"name": "deepseek"}
    req.query = {}
    resp = await ui_channel._handle_provider_models(req)

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["provider"] == "deepseek"
    assert body["models"] == ["deepseek/deepseek-chat", "deepseek/deepseek-reasoner"]
    assert body["cached"] is False


async def test_handle_provider_models_rejects_unknown_provider(ui_channel: UiChannel) -> None:
    req = MagicMock(spec=web.Request)
    req.match_info = {"name": "not-a-provider"}
    req.query = {}
    resp = await ui_channel._handle_provider_models(req)

    assert resp.status == 400
    assert "unknown provider" in json.loads(resp.text)["error"]


async def test_handle_provider_models_surfaces_fetch_error(
    ui_channel: UiChannel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config()
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(ui_channel_mod.config_loader, "get_config_path", lambda: config_path)
    monkeypatch.setattr(ui_channel_mod.config_loader, "load_config", lambda _path=None: config)
    monkeypatch.setattr(ui_channel_mod, "read_model_cache", lambda *_a, **_k: None)

    async def fake_fetch(*_a, **_k):
        raise ui_channel_mod.ModelFetchError("bad api key")

    monkeypatch.setattr(ui_channel_mod, "fetch_models_to_cache", fake_fetch)

    req = MagicMock(spec=web.Request)
    req.match_info = {"name": "deepseek"}
    req.query = {}
    resp = await ui_channel._handle_provider_models(req)

    assert resp.status == 502
    assert json.loads(resp.text)["error"] == "bad api key"


async def test_handle_provider_test_reports_success(
    ui_channel: UiChannel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config()
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(ui_channel_mod.config_loader, "get_config_path", lambda: config_path)
    monkeypatch.setattr(ui_channel_mod.config_loader, "load_config", lambda _path=None: config)

    captured: dict[str, Any] = {}

    async def fake_fetch(cfg, provider):
        captured["api_key"] = cfg.providers.deepseek.api_key
        return _model_cache(provider)

    monkeypatch.setattr(ui_channel_mod, "fetch_provider_models", fake_fetch)

    req = MagicMock(spec=web.Request)
    req.match_info = {"name": "deepseek"}
    req.json = AsyncMock(return_value={"api_key": "sk-transient"})
    resp = await ui_channel._handle_provider_test(req)

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body == {"ok": True, "message": "Connection succeeded", "model_count": 2}
    # Transient credential applied to the in-memory config (not persisted).
    assert captured["api_key"] == "sk-transient"


async def test_handle_provider_test_reports_failure(
    ui_channel: UiChannel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config()
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(ui_channel_mod.config_loader, "get_config_path", lambda: config_path)
    monkeypatch.setattr(ui_channel_mod.config_loader, "load_config", lambda _path=None: config)

    async def fake_fetch(*_a, **_k):
        raise ui_channel_mod.ModelFetchError("unauthorized")

    monkeypatch.setattr(ui_channel_mod, "fetch_provider_models", fake_fetch)

    req = MagicMock(spec=web.Request)
    req.match_info = {"name": "deepseek"}
    req.json = AsyncMock(return_value={})
    resp = await ui_channel._handle_provider_test(req)

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["ok"] is False
    assert body["message"] == "unauthorized"
