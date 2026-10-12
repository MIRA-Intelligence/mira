from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mira_engine.projects import ProjectRegistry
from mira_engine.workflows.http import WorkflowHttpApi


@pytest.fixture
async def workflow_client(tmp_path: Path) -> AsyncIterator[TestClient]:
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    registry = ProjectRegistry(
        projects_root,
        workspace_path=tmp_path / "workspace.json",
        legacy_index_path=tmp_path / "project-dirs.json",
    )
    registry.create_project(project_id="PRJ-api", display_name="API project")
    api = WorkflowHttpApi(registry)
    app = web.Application()
    api.add_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await api.close()
        await client.close()


async def test_contract_and_direct_run_creation(workflow_client: TestClient) -> None:
    contract = await workflow_client.get("/api/workflow/v1/contract")
    assert contract.status == 200
    assert (await contract.json())["features"] == [
        "project_event_replay",
        "run_commands",
        "structured_interactions",
        "direct_module_entry",
    ]

    response = await workflow_client.post(
        "/api/workflow/v1/projects/PRJ-api/runs",
        json={
            "command_id": "cmd-direct",
            "module_type": "delivery",
            "module_data": {"intent": "polish"},
        },
    )
    assert response.status == 201
    receipt = await response.json()

    snapshot_response = await workflow_client.get("/api/workflow/v1/projects/PRJ-api")
    snapshot = await snapshot_response.json()
    assert snapshot["runs"][0]["run_id"] == receipt["run_id"]
    assert snapshot["runs"][0]["module_type"] == "delivery"


@pytest.mark.parametrize("journal_id", ["radiology", "media"])
async def test_journal_catalog_and_selection_round_trip(workflow_client: TestClient, journal_id):
    response = await workflow_client.get("/api/workflow/v1/journals")
    assert response.status == 200
    catalog = await response.json()
    assert len(catalog["journals"]) == 7
    selected = next(item for item in catalog["journals"] if item["id"] == journal_id)
    assert selected["verification_status"] == "verified"
    response = await workflow_client.post("/api/workflow/v1/projects/PRJ-api/runs", json={
        "command_id": "journal", "module_type": "delivery",
        "module_data": {"intent": "write", "delivery_format": "paper_article", "target_journal": journal_id},
    })
    assert response.status == 201
    run_id = (await response.json())["run_id"]
    snapshot = await (await workflow_client.get("/api/workflow/v1/projects/PRJ-api")).json()
    run = next(run for run in snapshot["runs"] if run["run_id"] == run_id)
    assert run["module_data"]["target_journal"] == journal_id
    assert run["module_data"]["journal_profile_version"] == catalog["profile_version"]
    invalid = await workflow_client.post("/api/workflow/v1/projects/PRJ-api/runs", json={
        "command_id": "invalid-journal", "module_type": "delivery",
        "module_data": {"delivery_format": "paper_article", "target_journal": "unknown"},
    })
    assert invalid.status == 422


async def test_websocket_replays_and_then_follows_live_events(
    workflow_client: TestClient,
) -> None:
    first = await workflow_client.post(
        "/api/workflow/v1/projects/PRJ-api/runs",
        json={"command_id": "cmd-first", "module_type": "initiation"},
    )
    assert first.status == 201

    ws = await workflow_client.ws_connect("/ws/workflow/v1")
    await ws.send_json(
        {
            "message_type": "subscribe",
            "workflow_contract_version": 1,
            "subscription_id": "sub-test",
            "project_id": "PRJ-api",
            "after_project_sequence": 0,
        }
    )
    replay = await ws.receive_json()
    subscribed = await ws.receive_json()
    assert replay["message_type"] == "workflow_event"
    assert replay["project_sequence"] == 1
    assert subscribed["message_type"] == "subscribed"

    second = await workflow_client.post(
        "/api/workflow/v1/projects/PRJ-api/runs",
        json={"command_id": "cmd-second", "module_type": "planning"},
    )
    assert second.status == 201
    live = await ws.receive_json()
    assert live["project_sequence"] == 2
    assert live["payload"]["module_type"] == "planning"
    await ws.close()


async def test_invalid_cursor_requires_snapshot(workflow_client: TestClient) -> None:
    ws = await workflow_client.ws_connect("/ws/workflow/v1")
    await ws.send_json(
        {
            "message_type": "subscribe",
            "project_id": "PRJ-api",
            "after_project_sequence": 99,
        }
    )
    message = await ws.receive_json()
    assert message["message_type"] == "snapshot_required"
    assert message["watermark"] == 0
    await ws.close()


async def test_artifact_import_and_content_round_trip(workflow_client: TestClient) -> None:
    project_response = await workflow_client.get("/api/workflow/v1/projects/PRJ-api")
    assert project_response.status == 200
    # Resolve the fixture's registered Project through the bound handler instance.
    route = next(
        route for route in workflow_client.server.app.router.routes()
        if route.resource.canonical == "/api/workflow/v1/projects/{project_id}"
    )
    api = route.handler.__self__
    ref = api.registry.resolve("PRJ-api")
    (ref.project_dir / "result.txt").write_text("verified result", encoding="utf-8")

    imported = await workflow_client.post(
        "/api/workflow/v1/projects/PRJ-api/artifacts",
        json={
            "command_id": "cmd-import",
            "source_path": "result.txt",
            "artifact_type": "result_report",
            "media_type": "text/plain",
        },
    )
    assert imported.status == 201
    artifact = (await imported.json())["artifact"]
    assert artifact["sha256"]

    content = await workflow_client.get(
        f"/api/workflow/v1/projects/PRJ-api/artifacts/{artifact['artifact_id']}/content"
    )
    assert content.status == 200
    assert await content.text() == "verified result"


async def test_project_policy_and_structured_interaction_routes(
    workflow_client: TestClient,
) -> None:
    snapshot = await (await workflow_client.get("/api/workflow/v1/projects/PRJ-api")).json()
    policy = await workflow_client.post(
        "/api/workflow/v1/projects/PRJ-api/commands",
        json={
            "command_id": "cmd-policy-http",
            "command_type": "set_handoff_policy",
            "expected_revision": snapshot["workflow"]["revision"],
            "payload": {"handoff_policy": {"mode": "auto", "rules": []}},
        },
    )
    assert policy.status == 200

    created = await workflow_client.post(
        "/api/workflow/v1/projects/PRJ-api/runs",
        json={"command_id": "cmd-run-http", "module_type": "planning", "enqueue": True},
    )
    run_id = (await created.json())["run_id"]
    route = next(
        route for route in workflow_client.server.app.router.routes()
        if route.resource.canonical == "/api/workflow/v1/projects/{project_id}"
    )
    api = route.handler.__self__
    service = api._service("PRJ-api")
    service.claim_run(run_id, lease_owner="executor:http-test")
    running = service.repository.load_run(run_id)

    signal = await workflow_client.post(
        f"/api/workflow/v1/projects/PRJ-api/runs/{run_id}/signals",
        json={
            "command_id": "signal-http",
            "signal_type": "interaction_requested",
            "attempt_id": running.attempt.current_attempt_id,
            "checkpoint_state": {"step": 2},
            "interaction": {
                "interaction_id": "interaction-http",
                "interaction_type": "approval",
                "title": "Approve plan",
                "prompt": "Continue?",
                "response_schema": {"type": "boolean"},
                "choices": [{"label": "Yes", "value": True}],
                "allow_freeform": False,
            },
        },
    )
    assert signal.status == 202
    waiting = await (await workflow_client.get(
        f"/api/workflow/v1/projects/PRJ-api/runs/{run_id}"
    )).json()
    checkpoint = await workflow_client.get(
        f"/api/workflow/v1/projects/PRJ-api/runs/{run_id}/checkpoints/{waiting['checkpoint_ref']}"
    )
    assert checkpoint.status == 200
    assert (await checkpoint.json())["state"] == {"step": 2}
