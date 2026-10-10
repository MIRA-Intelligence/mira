"""Run-owned experiment records and a read-only legacy UI projection."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import ModuleRun
from .repository import JsonWorkflowRepository


class ExperimentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    title: str = Field(min_length=1, max_length=1000)
    status: Literal["pending", "running", "completed", "failed", "skipped"]
    question: str = Field(default="", max_length=20000)
    hypothesis: str = Field(default="", max_length=20000)
    prediction: str = Field(default="", max_length=20000)
    method: str = Field(default="", max_length=50000)
    results: dict[str, Any] = Field(default_factory=dict, description="metrics: nested metric objects; findings: text; artifacts: project-relative file paths")
    conclusion: str = Field(default="", max_length=50000)
    next: str = Field(default="", max_length=20000)
    progress: dict[str, Any] = Field(default_factory=dict)
    parent: str | None = None
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    theoretical_proof: str = Field(default="", max_length=20000)
    isolation_test: dict[str, Any] = Field(default_factory=dict)
    post_mortem: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_display_fields(self) -> ExperimentRecord:
        # These fields are rendered as text by the existing experiment detail UI.
        for data, keys in (
            (self.results, ("findings",)),
            (self.progress, ("current_metric",)),
            (self.isolation_test, ("control", "treatment", "isolated_variable", "result")),
            (self.post_mortem, ("residual_analysis", "implementation_fidelity")),
        ):
            for key in keys:
                if key in data and not isinstance(data[key], str):
                    raise ValueError(f"{key} must be text")
        if "metrics" in self.results and not isinstance(self.results["metrics"], dict):
            raise ValueError("results.metrics must be an object")
        whys = self.post_mortem.get("five_whys", [])
        if not isinstance(whys, list) or not all(isinstance(item, str) for item in whys):
            raise ValueError("post_mortem.five_whys must be an array of text")
        # Reject non-finite nested metrics and bound record size before persistence.
        if len(json.dumps(self.model_dump(), allow_nan=False).encode()) > 256_000:
            raise ValueError("experiment record exceeds 256 KB; store large results as artifacts")
        return self


def scoped_experiment_id(run_id: str, experiment_id: str) -> str:
    return f"workflow:{run_id}:{experiment_id}"


def project_experiments(repository: JsonWorkflowRepository) -> list[dict[str, Any]]:
    """Adapt records to the existing Experiment UI without writing task_plan.json.

    Unregistered historical Runs get one explicitly labelled summary, not one
    invented experiment per Artifact. Missing fields remain missing.
    """
    experiments: list[dict[str, Any]] = []
    for run in repository.list_runs():
        if run.module_type != "experiment":
            continue
        records = run.module_data.get("experiments")
        if not isinstance(records, list) or not records:
            records = [_run_summary(repository, run)]
        for raw in records:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            item = deepcopy(raw)
            local_id = str(item["id"])
            item.update(
                id=scoped_experiment_id(run.run_id, local_id),
                experiment_id=local_id,
                workflow_run_id=run.run_id,
                workflow_run_label=run.label,
            )
            if item.get("parent"):
                item["parent"] = scoped_experiment_id(run.run_id, item["parent"])
            experiments.append(item)
    return experiments


def _run_summary(repository: JsonWorkflowRepository, run: ModuleRun) -> dict[str, Any]:
    refs = []
    for artifact_id in run.outputs:
        try:
            artifact = repository.load_artifact(artifact_id)
        except FileNotFoundError:
            continue
        refs.append({"ref_id": artifact_id, "artifact": artifact.payload_ref,
                     "relevance": artifact.label or artifact.artifact_type})
    status = {"completed": "completed", "failed": "failed", "cancelled": "skipped",
              "running": "running"}.get(run.status, "pending")
    return {
        "id": "run-summary", "title": f"{run.label} · 运行汇总 / Run summary",
        "status": status, "question": run.module_data.get("intent", ""),
        "results": {"findings": run.module_data.get("completion_summary", ""),
                    "artifacts": [ref["artifact"] for ref in refs]},
        "evidence_refs": refs,
        "guard_warnings": ["此条是 Run 汇总，尚未登记独立实验 / Individual experiments are not registered."],
    }
