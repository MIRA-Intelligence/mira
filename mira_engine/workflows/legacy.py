"""One-time import of pre-modular Projects (``task_plan.json``) into ModuleRuns.

Releases before the modular workflow kept one linear Research -> Plan ->
Experiment -> Result record in ``<project>/task_plan.json``. The modular UI only
reads ModuleRuns, so without this import an old Project opens empty and its
research can neither be viewed nor handed to a new Run.

The import is additive and idempotent: ``task_plan.json`` is never modified (a
copy is kept under ``.mira/legacy/``), each populated stage becomes one
completed, externally-imported Run with typed Artifacts, and a marker file
makes later opens a no-op.
"""

from __future__ import annotations

import copy
import json
import math
import mimetypes
import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import ValidationError

from mira_engine.task_plan.guardrails import reconcile_task_plan_data

from .experiments import ExperimentRecord, ReplanDecision, parent_id_of
from .models import (
    Actor,
    AppliedCommand,
    Artifact,
    ArtifactInput,
    InitiationResearchResult,
    ModuleRun,
    ResearchDirection,
    ResearchReference,
    Transition,
    WorkflowEvent,
    new_id,
    utc_now,
)
from .repository import JsonWorkflowRepository

LEGACY_PLAN_FILENAME = "task_plan.json"
LEGACY_MARKER_FILENAME = "legacy_import.json"
LEGACY_IMPORT_VERSION = 2  # 2: imported Runs carry parent_run_id / upstream_selection
_ACTOR_ID = "legacy-task-plan"
_EXPERIMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_TEXT_EXPERIMENT_FIELDS = ("question", "hypothesis", "prediction", "method", "conclusion", "next")
_MAX_SURVEY = 50000


def _text(value: Any, limit: int | None = None) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = text.strip()
    return text[:limit] if limit else text


def _finite(value: Any) -> Any:
    """Legacy files may contain NaN/Infinity, which the strict models reject."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite(item) for item in value]
    return value


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def legacy_import_done(repository: JsonWorkflowRepository) -> bool:
    marker = _read_marker(repository)
    return bool(marker and marker.get("status") == "complete")


def _lineage(module_type: str, by_type: dict[str, ModuleRun]) -> dict[str, Any]:
    """The branch fields the UI uses to scope experiments / deliveries to their upstream Run.

    The UI lists an experiment Run only under the research direction that names it as
    ``parent_run_id``; without these fields an imported Run falls out of every scope and
    the experiment list is empty even though the Run exists.
    """
    if module_type == "experiment" and "initiation" in by_type:
        parent = by_type["initiation"]
        return {
            "parent_run_id": parent.run_id,
            "upstream_selection": {
                "module_type": "initiation", "run_id": parent.run_id, "item_id": "legacy-core-question",
                "label": _text(parent.module_data.get("intent"), 500) or parent.label,
            },
        }
    if module_type == "delivery" and "experiment" in by_type:
        parent = by_type["experiment"]
        # No item_id: the legacy result summarized the whole experiment sequence.
        return {
            "parent_run_id": parent.run_id,
            "upstream_selection": {"module_type": "experiment", "run_id": parent.run_id, "label": parent.label},
        }
    return {}


def link_legacy_experiments(
    plan: dict[str, Any], records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Point task_plan.json experiments at their imported Run; drop the duplicate projections.

    ``records`` are ``project_experiments`` items. Legacy items keep their own id and
    payload (snapshots, reconciled fields); only Run links are added to them.
    """
    legacy = [item for item in plan.get("experiments") or [] if isinstance(item, dict) and item.get("id")]
    imported = {
        (item["workflow_run_id"], item["experiment_id"]): item
        for item in records
        if item.get("legacy_import") and item.get("experiment_id")
    }
    by_local_id = {item["experiment_id"]: item for item in imported.values()}
    linked: set[str] = set()
    for item in legacy:
        match = by_local_id.get(item["id"])
        if match is not None:
            item.update(
                workflow_run_id=match["workflow_run_id"],
                workflow_run_label=match["workflow_run_label"],
                experiment_id=item["id"],
            )
            linked.add(match["id"])
    return [item for item in records if not (item.get("legacy_import") and item["id"] in linked)]


def _marker_path(repository: JsonWorkflowRepository) -> Path:
    return repository.meta_dir / LEGACY_MARKER_FILENAME


def _read_marker(repository: JsonWorkflowRepository) -> dict[str, Any] | None:
    try:
        data = json.loads(_marker_path(repository).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _load_legacy_plan(project_dir: Path) -> dict[str, Any] | None:
    path = project_dir / LEGACY_PLAN_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        # Same normalization the legacy reader applies (unique Exp### ids, metrics from
        # result files), so imported ids match what /api/plan serves for the same file.
        normalized, _ = reconcile_task_plan_data(copy.deepcopy(data), project_dir)
        return normalized
    except Exception:  # noqa: BLE001 - fall back to the file as written
        return data


def _reference(raw: Any, index: int) -> ResearchReference | None:
    if not isinstance(raw, dict):
        text = _text(raw, 1000)
        return ResearchReference(id=f"R{index}", title=text) if text else None
    url = _text(raw.get("url"), 4000) or None
    title = _text(raw.get("title"), 1000) or url or _text(raw.get("doi"), 500)
    if not title:
        return None
    return ResearchReference(
        id=_text(raw.get("id"), 64) or f"R{index}",
        title=title,
        authors=_text(raw.get("authors"), 2000) or None,
        year=_text(raw.get("year"), 32) or None,
        source=_text(raw.get("source"), 200) or None,
        venue=_text(raw.get("venue"), 500) or None,
        url=url,
        doi=_text(raw.get("doi"), 500) or None,
        summary=_text(raw.get("summary"), 8000) or None,
        relevance=_text(raw.get("relevance"), 4000) or None,
    )


def _research_result(plan: dict[str, Any]) -> InitiationResearchResult | None:
    research = plan.get("research")
    if not isinstance(research, dict):
        return None
    references = [
        ref
        for index, raw in enumerate(research.get("references") or [], start=1)
        if (ref := _reference(raw, index)) is not None
    ]
    seen: set[str] = set()
    for ref in references:  # ids key the citations in the survey; keep them unique
        while ref.id in seen:
            ref.id += "'"
        seen.add(ref.id)
    notes = [_text(note, 8000) for note in (research.get("notes") or []) if _text(note)]
    survey = _text(research.get("survey"))
    if not (references or notes or survey):
        return None
    question = _text(plan.get("core_question"), 500) or _text(plan.get("title"), 500)
    directions = []
    if question:
        # Legacy research ended in one project question, never a direction choice.
        directions.append(
            ResearchDirection(
                id="legacy-core-question",
                title=question,
                rationale=_text(survey or "; ".join(notes), 8000) or question,
                next_step="Plan or run experiments on the imported core question.",
            )
        )
    return InitiationResearchResult(
        survey=survey[:_MAX_SURVEY],
        references=references[:500],
        notes=notes[:500],
        directions=directions,
    )


def _research_report(plan: dict[str, Any], result: InitiationResearchResult) -> str:
    research = plan.get("research") if isinstance(plan.get("research"), dict) else {}
    lines = [f"# {_text(plan.get('title')) or 'Imported research'}", ""]
    if plan.get("core_question"):
        lines += [f"**Core question:** {_text(plan['core_question'])}", ""]
    lines += ["## Survey", "", _text(research.get("survey")) or "_(none)_", ""]
    if result.notes:
        lines += ["## Notes", ""] + [f"- {note}" for note in result.notes] + [""]
    if result.references:
        lines += ["## References", ""]
        for ref in result.references:
            meta = " · ".join(filter(None, [ref.authors, ref.venue, ref.year]))
            lines.append(f"- [{ref.id}] {ref.title}" + (f" — {meta}" if meta else ""))
            if ref.url:
                lines.append(f"  - {ref.url}")
            if ref.summary:
                lines.append(f"  - {ref.summary}")
    return "\n".join(lines).rstrip() + "\n"


_DISPLAY_TEXT = (
    ("results", ("findings",)),
    ("progress", ("current_metric",)),
    ("repro", ("script_path",)),
    ("tests", ("summary",)),
    ("isolation_test", ("control", "treatment", "isolated_variable", "result")),
    ("post_mortem", ("residual_analysis", "implementation_fidelity")),
)


def _coerce_display_text(record: dict[str, Any]) -> None:
    """Old records stored structured values where the Run contract renders text."""
    for block, keys in _DISPLAY_TEXT:
        for key in keys:
            if key in record[block] and not isinstance(record[block][key], str):
                record[block][key] = _text(record[block][key])
    whys = record["post_mortem"].get("five_whys")
    if whys is not None:
        items = whys if isinstance(whys, list) else [whys]
        record["post_mortem"]["five_whys"] = [_text(item) for item in items if _text(item)]


def _experiment_record(raw: Any, index: int, notes: list[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    raw = _finite(raw)
    experiment_id = _text(raw.get("id"), 100)
    if not _EXPERIMENT_ID.match(experiment_id):
        experiment_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", experiment_id).strip("-._")[:100]
        if not _EXPERIMENT_ID.match(experiment_id):
            experiment_id = f"Exp{index:03d}"
    status = raw.get("status")
    conclusion = _text(raw.get("conclusion"), 50000)
    # The modular executor cannot resume a half-finished legacy experiment, so
    # a Run built from one only holds terminal records.
    if status == "running":
        status = "failed"
        marker = "Interrupted when the Project was upgraded to modular workflows."
        conclusion = f"{conclusion}\n\n{marker}".strip()
        notes.append(f"{experiment_id}: running -> failed")
    elif status == "pending" or status not in {"completed", "failed", "skipped"}:
        status = "skipped"
        notes.append(f"{experiment_id}: {raw.get('status') or 'unknown'} -> skipped")
    record: dict[str, Any] = {
        "id": experiment_id,
        "title": _text(raw.get("title"), 1000) or experiment_id,
        "status": status,
        "conclusion": conclusion,
    }
    for key in _TEXT_EXPERIMENT_FIELDS:
        if key != "conclusion":
            record[key] = _text(raw.get(key), 50000)
    for key in ("results", "progress", "isolation_test", "post_mortem", "repro", "tests"):
        value = raw.get(key)
        record[key] = value if isinstance(value, dict) else {}
    results = record["results"]
    if "findings" in results:
        results["findings"] = _text(results["findings"])
    if not isinstance(results.get("artifacts", []), list):
        results["artifacts"] = []
    results["artifacts"] = [str(path) for path in results.get("artifacts", []) if isinstance(path, str)]
    if not isinstance(results.get("metrics", {}), dict):
        results.pop("metrics")
    _coerce_display_text(record)
    refs = raw.get("evidence_refs")
    record["evidence_refs"] = [ref for ref in refs if isinstance(ref, dict)] if isinstance(refs, list) else []
    record["theoretical_proof"] = _text(raw.get("theoretical_proof"), 20000)
    record["commit"] = _text(raw.get("commit"), 1000)
    parent = parent_id_of(experiment_id)
    if parent:
        record["parent"] = parent
    if isinstance(raw.get("replan"), dict):
        try:
            record["replan"] = ReplanDecision.model_validate(raw["replan"]).model_dump(mode="json")
        except ValidationError:
            pass
    try:
        return ExperimentRecord.model_validate(record).model_dump(mode="json")
    except ValidationError:
        notes.append(f"{experiment_id}: kept title/status only (record did not fit the contract)")
        fallback = {
            "id": experiment_id,
            "title": record["title"],
            "status": status,
            "conclusion": conclusion[:50000],
            "results": {"findings": _text(results.get("findings"), 20000)},
        }
        return ExperimentRecord.model_validate(fallback).model_dump(mode="json")


def _experiment_records(plan: dict[str, Any], notes: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(plan.get("experiments") or [], start=1):
        record = _experiment_record(raw, index, notes)
        if record is None or record["id"] in seen:
            continue
        seen.add(record["id"])
        records.append(record)
    # A child whose parent was dropped would fail later parent checks.
    for record in records:
        if record.get("parent") and record["parent"] not in seen:
            record["parent"] = None
    return records


def _automation_policy(plan: dict[str, Any]) -> dict[str, Any] | None:
    policy = plan.get("automation_policy")
    if not isinstance(policy, dict):
        return None
    converted = dict(policy)
    for old, new in (("max_experiments", "maxExperiments"), ("max_branch_depth", "maxBranchDepth")):
        if old in converted and new not in converted:
            converted[new] = converted.pop(old)
    return converted


class LegacyProjectImporter:
    """Materialize a legacy ``task_plan.json`` as completed, imported ModuleRuns."""

    def __init__(self, repository: JsonWorkflowRepository):
        self.repository = repository

    def pending(self) -> bool:
        if legacy_import_done(self.repository):
            return False
        return (self.repository.project_dir / LEGACY_PLAN_FILENAME).is_file()

    def import_if_needed(self) -> bool:
        """Return True when this call imported the Project. Never raises."""
        if not self.pending():
            self._repair_lineage()
            return False
        try:
            return self._import()
        except Exception:  # noqa: BLE001 - an unreadable legacy file must not block the Project
            logger.exception("Legacy workflow import failed for {}", self.repository.project_id)
            return False

    # ------------------------------------------------------------------

    def _import(self) -> bool:
        repo = self.repository
        with repo.transaction():
            if legacy_import_done(repo):
                return False
            plan = _load_legacy_plan(repo.project_dir)
            if plan is None:
                return False
            self._discard_partial_import()
            plan_path = repo.project_dir / LEGACY_PLAN_FILENAME
            backup = repo.meta_dir / "legacy" / LEGACY_PLAN_FILENAME
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                shutil.copy2(plan_path, backup)

            workflow = repo.load_workflow()
            started = _parse_time(plan.get("started_at"))
            if started is None:
                started = datetime.fromtimestamp(plan_path.stat().st_mtime, UTC)
            finished = (
                _parse_time(plan.get("completed_at"))
                or _parse_time(plan.get("updated_at"))
                or datetime.fromtimestamp(plan_path.stat().st_mtime, UTC)
            )
            finished = max(finished, started + timedelta(seconds=8))

            artifacts: list[tuple[Artifact, bytes, str]] = []
            runs: list[ModuleRun] = []
            normalized: list[str] = []
            previous_outputs: list[str] = []

            def publish(
                artifact_type: str, label: str, content: bytes, suffix: str, media_type: str,
                at: str, derived_from: list[str],
            ) -> str:
                artifact = Artifact(
                    artifact_id=new_id(), artifact_type=artifact_type, label=label,
                    media_type=media_type, byte_size=0, sha256="", payload_ref="",
                    created_at=at, created_by=Actor(kind="external_import", id=_ACTOR_ID),
                    derived_from=derived_from,
                )
                artifacts.append((artifact, content, suffix))
                return artifact.artifact_id

            def add_run(
                module_type: str, label: str, order: int, outputs: list[str],
                module_data: dict[str, Any],
            ) -> None:
                nonlocal previous_outputs
                module_data = {**module_data, **_lineage(module_type, {r.module_type: r for r in runs})}
                at = _iso(started + (finished - started) * order / 4)
                sequence = workflow.last_project_sequence + len(runs) + 1
                event_id = new_id()
                run_id = new_id()
                runs.append(ModuleRun(
                    run_id=run_id, project_id=repo.project_id, module_type=module_type,  # type: ignore[arg-type]
                    label=label, status="completed", created_at=at, updated_at=at,
                    created_by=Actor(kind="external_import", id=_ACTOR_ID),
                    inputs=[ArtifactInput(artifact_id=item) for item in previous_outputs],
                    outputs=outputs,
                    conversation_key=f"ui:{repo.project_id}:run:{run_id}",
                    last_project_sequence=sequence, last_run_sequence=1,
                    last_transition=Transition(
                        event_id=event_id, event_type="run.imported",
                        **{"from": None}, to="completed", occurred_at=at,
                    ),
                    applied_commands=[AppliedCommand(
                        command_id=f"legacy-import:{repo.project_id}:{module_type}",
                        command_type="import_legacy", result_revision=1,
                    )],
                    module_data={
                        **module_data,
                        "legacy_import": {
                            "version": LEGACY_IMPORT_VERSION, "source": LEGACY_PLAN_FILENAME,
                            "stage": module_type, "imported_at": utc_now(),
                        },
                    },
                ))
                previous_outputs = outputs or previous_outputs

            question = _text(plan.get("core_question"), 4000)
            at_start, at_end = _iso(started), _iso(finished)

            # Research -> initiation
            research = _research_result(plan)
            if research is not None:
                json_id = publish(
                    "initiation_research_result", "Imported research result",
                    json.dumps(research.model_dump(mode="json"), ensure_ascii=False, indent=2).encode(),
                    ".json", "application/json", at_start, [],
                )
                md_id = publish(
                    "research_report", "Imported research report",
                    _research_report(plan, research).encode(), ".md", "text/markdown", at_start, [json_id],
                )
                add_run("initiation", "Initiation (imported)", 0, [json_id, md_id], {
                    "intent": question,
                    "literature_review": {"enabled": bool(research.references), "sources": []},
                    "research_result": research.model_dump(mode="json"),
                    "completion_summary": _text(research.survey, 500),
                    "suggested_handoffs": [],
                })

            # Interactive plan -> planning
            legacy_plan = plan.get("plan")
            if isinstance(legacy_plan, dict) and legacy_plan.get("draft"):
                approved = legacy_plan.get("phase") == "approved"
                plan_id = publish(
                    "plan_draft", "Imported plan" if approved else "Imported plan draft (not approved)",
                    json.dumps(_finite(legacy_plan), ensure_ascii=False, indent=2).encode(),
                    ".json", "application/json", at_start, previous_outputs[:1],
                )
                add_run("planning", "Planning (imported)", 1, [plan_id], {
                    "intent": question, "legacy_plan": _finite(legacy_plan),
                    "plan_approved": approved,
                    "completion_summary": _text(legacy_plan.get("draft"), 500),
                    "suggested_handoffs": [],
                })

            # Experiments -> experiment
            records = _experiment_records(plan, normalized)
            if records:
                records_id = publish(
                    "experiment_records", "Imported experiment records",
                    json.dumps({"experiments": records}, ensure_ascii=False, indent=2).encode(),
                    ".json", "application/json", at_end, previous_outputs[:1],
                )
                data: dict[str, Any] = {
                    "intent": question, "experiments": records,
                    "experiment_execution_mode": "auto",
                    "experiment_contract_version": 2 if plan.get("contract_version") == 2 else 1,
                    "completion_summary": f"{len(records)} experiment(s) imported from task_plan.json",
                    "suggested_handoffs": [],
                }
                policy = _automation_policy(plan)
                if policy:
                    data["automation_policy"] = policy
                add_run("experiment", "Experiment (imported)", 2, [records_id], data)

            # Result -> delivery
            result = plan.get("result")
            if isinstance(result, dict) and result:
                outputs: list[str] = []
                output_path = _text(result.get("output_path"))
                deliverable = self._project_file(output_path)
                if deliverable is not None:
                    media = mimetypes.guess_type(deliverable.name)[0] or "application/octet-stream"
                    outputs.append(publish(
                        "deliverable", deliverable.name, deliverable.read_bytes(),
                        deliverable.suffix, media, at_end, previous_outputs[:1],
                    ))
                else:
                    outputs.append(publish(
                        "delivery_result", "Imported result summary",
                        json.dumps(_finite(result), ensure_ascii=False, indent=2).encode(),
                        ".json", "application/json", at_end, previous_outputs[:1],
                    ))
                add_run("delivery", "Delivery (imported)", 3, outputs, {
                    "intent": question, "output_path": output_path,
                    "output_type": _text(result.get("output_type")),
                    "delivery_result": _finite(result),
                    "completion_summary": _text(result.get("summary"), 2000),
                    "suggested_handoffs": [],
                })

            marker = {
                "version": LEGACY_IMPORT_VERSION, "status": "in_progress", "source": LEGACY_PLAN_FILENAME,
                "run_ids": [run.run_id for run in runs],
                "artifact_ids": [artifact.artifact_id for artifact, _, _ in artifacts],
            }
            self._write_marker(marker)
            for artifact, content, suffix in artifacts:
                repo.publish_artifact(artifact, content, suffix)
            events = []
            for run in runs:
                assert run.last_transition is not None
                events.append(WorkflowEvent(
                    event_id=run.last_transition.event_id, project_id=repo.project_id,
                    project_sequence=run.last_project_sequence, run_id=run.run_id, run_sequence=1,
                    event_type="run.imported", occurred_at=run.created_at,
                    project_revision=workflow.revision + len(events) + 1, run_revision=run.revision,
                    payload={"module_type": run.module_type, "label": run.label, "status": "completed",
                             "source": LEGACY_PLAN_FILENAME},
                ))
                repo.save_run(run)
            for event in events:
                repo.append_event(event)
            if runs:
                workflow.revision += len(runs)
                workflow.last_project_sequence += len(runs)
                if workflow.selected_run_id is None:
                    workflow.selected_run_id = runs[-1].run_id
            repo.refresh_index(workflow)
            repo.save_workflow(workflow)
            self._write_marker({**marker, "status": "complete", "completed_at": utc_now(),
                                "normalized": normalized})
            logger.info(
                "Imported legacy Project {} as {} Run(s)",
                repo.project_id, ", ".join(run.module_type for run in runs) or "no",
            )
            return True

    def _repair_lineage(self) -> None:
        """Projects imported before version 2 have Runs the UI cannot scope; link them once."""
        marker = _read_marker(self.repository)
        if not marker or marker.get("status") != "complete" or int(marker.get("version") or 1) >= 2:
            return
        repo = self.repository
        try:
            with repo.transaction():
                marker = _read_marker(repo)
                if not marker or int(marker.get("version") or 1) >= 2:
                    return
                runs = []
                for run_id in marker.get("run_ids") or []:
                    try:
                        runs.append(repo.load_run(str(run_id)))
                    except (FileNotFoundError, ValueError):
                        continue
                by_type = {run.module_type: run for run in runs}
                for run in runs:
                    extra = _lineage(run.module_type, by_type)
                    if extra and "parent_run_id" not in run.module_data:
                        run.module_data.update(extra)
                        repo.save_run(run)
                self._write_marker({**marker, "version": LEGACY_IMPORT_VERSION})
        except Exception:  # noqa: BLE001 - repairing lineage must never block the Project
            logger.exception("Legacy lineage repair failed for {}", repo.project_id)

    def _project_file(self, relative: str) -> Path | None:
        if not relative:
            return None
        try:
            path = (self.repository.project_dir / relative).resolve()
            path.relative_to(self.repository.project_dir)
        except (OSError, ValueError):
            return None
        return path if path.is_file() and path.stat().st_size <= 64 * 1024 * 1024 else None

    def _write_marker(self, marker: dict[str, Any]) -> None:
        path = _marker_path(self.repository)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _discard_partial_import(self) -> None:
        """A crash between marker and completion leaves unreferenced half-written ids."""
        marker = _read_marker(self.repository)
        if not marker or marker.get("status") == "complete":
            return
        for run_id in marker.get("run_ids") or []:
            shutil.rmtree(self.repository.modules_dir / str(run_id), ignore_errors=True)
        for artifact_id in marker.get("artifact_ids") or []:
            shutil.rmtree(self.repository.artifacts_dir / str(artifact_id), ignore_errors=True)
