from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from mira_engine.cli.workflow import workflow_app

runner = CliRunner()


def project_dir(tmp_path: Path) -> Path:
    project = tmp_path / "PRJ-cli"
    meta = project / ".mira"
    meta.mkdir(parents=True)
    (meta / "project.json").write_text(
        json.dumps({"schema_version": 1, "id": "PRJ-cli", "display_name": "CLI"}),
        encoding="utf-8",
    )
    return project


def test_cli_creates_lists_and_replays_direct_entry_run(tmp_path: Path) -> None:
    project = project_dir(tmp_path)
    created = runner.invoke(
        workflow_app,
        ["run", "create", str(project), "delivery", "--intent", "polish", "--json"],
    )
    assert created.exit_code == 0, created.output
    receipt = json.loads(created.output)
    assert receipt["run_id"]

    listed = runner.invoke(workflow_app, ["run", "list", str(project), "--json"])
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    assert payload["runs"][0]["module_type"] == "delivery"
    assert payload["runs"][0]["module_data"]["intent"] == "polish"

    events = runner.invoke(workflow_app, ["events", str(project)])
    assert events.exit_code == 0, events.output
    event = json.loads(events.output.strip())
    assert event["event_type"] == "run.created"
