"""CLI adapter for the same modular WorkflowService used by the UI API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import typer
from rich.console import Console
from rich.table import Table

from mira_engine.workflows import JsonWorkflowRepository, WorkflowService

workflow_app = typer.Typer(help="Inspect and control modular Project workflows.")
run_app = typer.Typer(help="Create and control ModuleRuns.")
workflow_app.add_typer(run_app, name="run")
console = Console()


def _service(project_dir: Path) -> WorkflowService:
    project_dir = project_dir.expanduser().resolve()
    project_path = project_dir / ".mira" / "project.json"
    if not project_path.is_file():
        raise typer.BadParameter(f"not a MIRA Project: {project_dir}")
    try:
        project = json.loads(project_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise typer.BadParameter(f"invalid Project metadata: {exc}") from exc
    project_id = project.get("id")
    if not isinstance(project_id, str) or not project_id:
        raise typer.BadParameter("project.json does not contain an id")
    return WorkflowService(JsonWorkflowRepository(project_dir, project_id))


def _print_json(value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", by_alias=True)
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


@workflow_app.command("show")
def show(project_dir: Path, json_output: bool = typer.Option(False, "--json")) -> None:
    """Show the Project workflow snapshot."""
    workflow, runs = _service(project_dir).snapshot()
    payload = {
        "workflow": workflow.model_dump(mode="json"),
        "runs": [run.model_dump(mode="json", by_alias=True) for run in runs],
    }
    if json_output:
        _print_json(payload)
        return
    console.print(
        f"[bold]{project_dir.name}[/bold]  {workflow.status}  "
        f"revision={workflow.revision} sequence={workflow.last_project_sequence}"
    )
    _render_runs(runs)


@run_app.command("list")
def list_runs(project_dir: Path, json_output: bool = typer.Option(False, "--json")) -> None:
    """List all independent ModuleRuns."""
    runs = _service(project_dir).repository.list_runs()
    if json_output:
        _print_json({"runs": [run.model_dump(mode="json", by_alias=True) for run in runs]})
        return
    _render_runs(runs)


@run_app.command("create")
def create_run(
    project_dir: Path,
    module_type: str = typer.Argument(help="initiation, planning, experiment, or delivery"),
    intent: str = typer.Option("", "--intent"),
    enqueue: bool = typer.Option(False, "--enqueue"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Create a direct-entry ModuleRun of any canonical type."""
    receipt = _service(project_dir).create_run(
        command_id=str(uuid4()),
        module_type=module_type,
        module_data={"intent": intent.strip()} if intent.strip() else {},
        enqueue=enqueue,
    )
    if json_output:
        _print_json(receipt)
    else:
        console.print(f"[green]Created[/green] {module_type} Run {receipt.run_id}")


@run_app.command("command")
def command_run(
    project_dir: Path,
    run_id: str,
    command_type: str,
    summary: str = typer.Option("", "--summary"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Apply enqueue, pause, resume, cancel, retry, or complete."""
    run = _service(project_dir).repository.load_run(run_id)
    receipt = _service(project_dir).command_run(
        run_id,
        command_id=str(uuid4()),
        command_type=command_type,
        expected_revision=run.revision,
        payload={"summary": summary.strip()} if summary.strip() else {},
    )
    if json_output:
        _print_json(receipt)
    else:
        console.print(f"[green]{receipt.disposition}[/green] {command_type} -> sequence {receipt.project_sequence}")


@workflow_app.command("events")
def events(
    project_dir: Path,
    after: int = typer.Option(0, "--after", min=0),
    limit: int = typer.Option(100, "--limit", min=1, max=500),
) -> None:
    """Print durable events after one Project cursor as JSON Lines."""
    for event in _service(project_dir).list_events(after=after, limit=limit):
        typer.echo(json.dumps(event.model_dump(mode="json"), ensure_ascii=False))


def _render_runs(runs: list[Any]) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Run")
    table.add_column("Module")
    table.add_column("Status")
    table.add_column("Revision", justify="right")
    table.add_column("Label")
    for run in runs:
        table.add_row(run.run_id, run.module_type, run.status, str(run.revision), run.label)
    console.print(table)
