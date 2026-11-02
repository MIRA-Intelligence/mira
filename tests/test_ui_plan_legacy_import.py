"""GET /api/plan serves a pre-modular Project's experiments once, as imported Runs."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from aiohttp import web

from tests.test_ui_channel import PLAN_FILENAME, ui_channel  # noqa: F401  (fixture)


async def test_plan_endpoint_does_not_duplicate_imported_experiments(ui_channel) -> None:  # noqa: F811
    session = "PRJ-9100"
    project_dir = ui_channel.projects_root / session
    project_dir.mkdir(parents=True)
    (project_dir / PLAN_FILENAME).write_text(
        json.dumps({
            "title": "old", "core_question": "Q?", "status": "completed",
            "research": {"survey": "s", "notes": [], "references": []},
            "experiments": [
                {"id": "Exp001", "title": "a", "status": "completed",
                 "results": {"findings": "f"}},
            ],
        }),
        encoding="utf-8",
    )

    req = MagicMock(spec=web.Request)
    req.query = {"session_id": session}
    body = json.loads((await ui_channel._handle_plan(req)).text)

    assert [item["id"] for item in body["experiments"]] == ["Exp001"]
    assert body["experiments"][0]["experiment_id"] == "Exp001"
    assert body["experiments"][0]["workflow_run_id"]
    assert body["research"]["survey"] == "s"
