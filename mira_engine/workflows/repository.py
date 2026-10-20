"""Crash-conscious JSON repository for modular workflow state."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from mira_engine.utils.locks import interprocess_lock

from .models import (
    Artifact,
    Checkpoint,
    ModuleIndexEntry,
    ModuleRun,
    ProjectWorkflow,
    WorkflowEvent,
)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


def _sync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _sync_parent(path)
    finally:
        tmp.unlink(missing_ok=True)


class JsonWorkflowRepository:
    """Own all workflow persistence below one registered Project directory."""

    def __init__(self, project_dir: Path, project_id: str):
        self.project_dir = project_dir.expanduser().resolve()
        self.project_id = project_id
        self.meta_dir = self.project_dir / ".mira"
        self.project_path = self.meta_dir / "project.json"
        self.modules_dir = self.meta_dir / "modules"
        self.events_path = self.meta_dir / "events.jsonl"
        self.artifacts_dir = self.meta_dir / "artifacts"

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        with interprocess_lock(self.project_path):
            yield

    def ensure_workflow(self) -> ProjectWorkflow:
        with self.transaction():
            project = _read_json(self.project_path)
            workflow = ProjectWorkflow.model_validate(project.get("workflow") or {})
            events = self._all_valid_events()
            known_event_ids = {event.event_id for event in events}
            event_watermark = max((event.project_sequence for event in events), default=0)
            event_revision = max((event.project_revision for event in events), default=0)
            for run in sorted(self.list_runs(), key=lambda item: item.last_project_sequence):
                transition = run.last_transition
                if (
                    transition is None
                    or transition.event_id in known_event_ids
                    or run.last_project_sequence <= event_watermark
                ):
                    continue
                repaired = WorkflowEvent(
                    event_id=transition.event_id,
                    project_id=self.project_id,
                    project_sequence=run.last_project_sequence,
                    run_id=run.run_id,
                    run_sequence=run.last_run_sequence,
                    event_type=transition.event_type,
                    occurred_at=transition.occurred_at,
                    project_revision=max(workflow.revision, event_revision),
                    run_revision=run.revision,
                    payload={
                        "from": transition.from_status,
                        "to": transition.to,
                        "recovered_from_snapshot": True,
                    },
                )
                self.append_event(repaired)
                events.append(repaired)
                known_event_ids.add(repaired.event_id)
                event_watermark = repaired.project_sequence
            workflow.last_project_sequence = max(
                workflow.last_project_sequence, event_watermark
            )
            workflow.revision = max(workflow.revision, event_revision)
            workflow.module_index = self._build_index()
            project["workflow"] = workflow.model_dump(mode="json", by_alias=True)
            project["schema_version"] = max(int(project.get("schema_version") or 1), 2)
            _atomic_json(self.project_path, project)
            return workflow

    def _all_valid_events(self) -> list[WorkflowEvent]:
        if not self.events_path.is_file():
            return []
        events: list[WorkflowEvent] = []
        with self.events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    events.append(WorkflowEvent.model_validate_json(line))
                except ValueError:
                    continue
        return events

    def load_project_document(self) -> dict[str, Any]:
        return _read_json(self.project_path)

    def load_workflow(self) -> ProjectWorkflow:
        project = self.load_project_document()
        return ProjectWorkflow.model_validate(project.get("workflow") or {})

    def save_workflow(self, workflow: ProjectWorkflow) -> None:
        project = self.load_project_document()
        project["schema_version"] = max(int(project.get("schema_version") or 1), 2)
        project["workflow"] = workflow.model_dump(mode="json", by_alias=True)
        _atomic_json(self.project_path, project)

    def module_path(self, run_id: str) -> Path:
        return self.modules_dir / run_id / "module.json"

    def load_run(self, run_id: str) -> ModuleRun:
        data = _read_json(self.module_path(run_id))
        if not data:
            raise FileNotFoundError(f"run not found: {run_id}")
        run = ModuleRun.model_validate(data)
        if run.project_id != self.project_id:
            raise ValueError("run belongs to another project")
        return run

    def save_run(self, run: ModuleRun) -> None:
        _atomic_json(
            self.module_path(run.run_id),
            run.model_dump(mode="json", by_alias=True),
        )

    def checkpoint_path(self, run_id: str, checkpoint_id: str) -> Path:
        return self.modules_dir / run_id / "checkpoints" / f"{checkpoint_id}.json"

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        if checkpoint.project_id != self.project_id:
            raise ValueError("checkpoint belongs to another project")
        _atomic_json(
            self.checkpoint_path(checkpoint.run_id, checkpoint.checkpoint_id),
            checkpoint.model_dump(mode="json"),
        )

    def load_checkpoint(self, run_id: str, checkpoint_id: str) -> Checkpoint:
        data = _read_json(self.checkpoint_path(run_id, checkpoint_id))
        if not data:
            raise FileNotFoundError(f"checkpoint not found: {checkpoint_id}")
        checkpoint = Checkpoint.model_validate(data)
        if checkpoint.project_id != self.project_id or checkpoint.run_id != run_id:
            raise ValueError("checkpoint belongs to another Run")
        return checkpoint

    def list_runs(self) -> list[ModuleRun]:
        if not self.modules_dir.is_dir():
            return []
        runs: list[ModuleRun] = []
        for path in sorted(self.modules_dir.glob("*/module.json")):
            try:
                run = ModuleRun.model_validate(_read_json(path))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if run.project_id == self.project_id:
                runs.append(run)
        return sorted(runs, key=lambda item: (item.created_at, item.run_id))

    def _build_index(self) -> list[ModuleIndexEntry]:
        return [
            ModuleIndexEntry(
                run_id=run.run_id,
                module_type=run.module_type,
                label=run.label,
                status=run.status,
                revision=run.revision,
                updated_at=run.updated_at,
            )
            for run in self.list_runs()
        ]

    def refresh_index(self, workflow: ProjectWorkflow) -> None:
        workflow.module_index = self._build_index()

    def append_event(self, event: WorkflowEvent) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def read_events(self, *, after: int = 0, limit: int = 100) -> list[WorkflowEvent]:
        if not self.events_path.is_file():
            return []
        events: list[WorkflowEvent] = []
        with self.events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    raw = json.loads(line)
                    event = WorkflowEvent.model_validate(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if event.project_sequence <= after:
                    continue
                events.append(event)
                if len(events) >= limit:
                    break
        return events

    def find_command_event(self, command_id: str) -> WorkflowEvent | None:
        if not self.events_path.is_file():
            return None
        found: WorkflowEvent | None = None
        with self.events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = WorkflowEvent.model_validate_json(line)
                except ValueError:
                    continue
                if event.command_id == command_id:
                    found = event
        return found

    def artifact_path(self, artifact_id: str) -> Path:
        return self.artifacts_dir / artifact_id / "artifact.json"

    def load_artifact(self, artifact_id: str) -> Artifact:
        data = _read_json(self.artifact_path(artifact_id))
        if not data:
            raise FileNotFoundError(f"artifact not found: {artifact_id}")
        return Artifact.model_validate(data)

    def list_artifacts(self) -> list[Artifact]:
        if not self.artifacts_dir.is_dir():
            return []
        artifacts: list[Artifact] = []
        for path in sorted(self.artifacts_dir.glob("*/artifact.json")):
            try:
                artifacts.append(Artifact.model_validate(_read_json(path)))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return sorted(artifacts, key=lambda item: (item.created_at, item.artifact_id))

    def publish_artifact(self, artifact: Artifact, content: bytes, suffix: str = ".bin") -> None:
        safe_suffix = suffix if suffix.startswith(".") and suffix[1:].isalnum() else ".bin"
        artifact_dir = self.artifacts_dir / artifact.artifact_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        payload_path = artifact_dir / f"payload{safe_suffix}"
        tmp = payload_path.with_name(f".{payload_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, payload_path)
            _sync_parent(payload_path)
        finally:
            tmp.unlink(missing_ok=True)
        artifact.payload_ref = str(payload_path.relative_to(self.project_dir))
        artifact.byte_size = len(content)
        artifact.sha256 = hashlib.sha256(content).hexdigest()
        _atomic_json(self.artifact_path(artifact.artifact_id), artifact.model_dump(mode="json"))

    def artifact_content_path(self, artifact: Artifact) -> Path:
        path = (self.project_dir / artifact.payload_ref).resolve()
        try:
            path.relative_to(self.project_dir)
        except ValueError as exc:
            raise ValueError("artifact payload escapes Project directory") from exc
        return path
