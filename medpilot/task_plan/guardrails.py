"""Guardrails for task_plan.json consistency and resilience."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

PLAN_FILENAME = "task_plan.json"
PLAN_SCHEMA_VERSION = 1

_VALID_PLAN_STATUS = {"in_progress", "completed", "failed"}
_VALID_EXPERIMENT_STATUS = {"pending", "running", "completed", "failed", "skipped"}
_EXP_ID_PATTERN = re.compile(r"(?i)^exp[-_ ]?(\d{1,4})$")


def _is_mapping(value: object) -> bool:
    return isinstance(value, dict)


def _normalize_experiment_id(value: object, fallback_idx: int) -> str:
    if isinstance(value, str):
        raw = value.strip()
        match = _EXP_ID_PATTERN.match(raw)
        if match:
            return f"Exp{int(match.group(1)):03d}"
        if raw:
            return raw
    return f"Exp{fallback_idx:03d}"


def _experiment_dirname(exp_id: str) -> str:
    match = _EXP_ID_PATTERN.match(exp_id)
    if match:
        return f"exp{int(match.group(1)):03d}"
    return exp_id.strip().lower()


def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _load_text(path: Path, limit: int = 1200) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    return text[:limit]


def _collect_artifacts(project_dir: Path, exp_id: str) -> list[str]:
    rel_paths: set[str] = set()
    exp_dirname = _experiment_dirname(exp_id)
    for base in (project_dir / "experiments" / exp_dirname, project_dir / "outputs" / exp_dirname):
        if not base.is_dir():
            continue
        for file in base.rglob("*"):
            if file.is_file():
                rel_paths.add(file.relative_to(project_dir).as_posix())
    return sorted(rel_paths)


def _recover_results(project_dir: Path, exp_id: str) -> dict[str, Any]:
    recovered: dict[str, Any] = {}
    exp_dirname = _experiment_dirname(exp_id)

    outputs_results = project_dir / "outputs" / exp_dirname / "results.json"
    outputs_payload = _load_json(outputs_results) if outputs_results.is_file() else None
    if _is_mapping(outputs_payload):
        if any(key in outputs_payload for key in ("metrics", "findings", "artifacts")):
            recovered.update(outputs_payload)
        else:
            recovered["metrics"] = outputs_payload

    metrics_json = project_dir / "experiments" / exp_dirname / "metrics.json"
    metrics_payload = _load_json(metrics_json) if metrics_json.is_file() else None
    if "metrics" not in recovered and metrics_payload is not None:
        recovered["metrics"] = metrics_payload

    if "findings" not in recovered:
        exp_dir = project_dir / "experiments" / exp_dirname
        if exp_dir.is_dir():
            md_files = sorted(exp_dir.glob("*.md"))
            if md_files:
                summary = _load_text(md_files[0])
                if summary:
                    recovered["findings"] = summary

    artifacts = _collect_artifacts(project_dir, exp_id)
    if artifacts:
        recovered["artifacts"] = artifacts

    return recovered


def _merge_results(existing: object, recovered: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing) if _is_mapping(existing) else {}
    if "metrics" not in merged and "metrics" in recovered:
        merged["metrics"] = recovered["metrics"]
    if "findings" not in merged and "findings" in recovered:
        merged["findings"] = recovered["findings"]

    merged_artifacts = set()
    if isinstance(merged.get("artifacts"), list):
        merged_artifacts.update(str(item) for item in merged["artifacts"])
    if isinstance(recovered.get("artifacts"), list):
        merged_artifacts.update(str(item) for item in recovered["artifacts"])
    if merged_artifacts:
        merged["artifacts"] = sorted(merged_artifacts)
    return merged


def lint_task_plan_data(data: object, project_dir: Path | None = None) -> list[str]:
    """Return structural issues found in a task plan object."""
    issues: list[str] = []
    if not _is_mapping(data):
        return ["task_plan root must be a JSON object"]

    experiments = data.get("experiments")
    if not isinstance(experiments, list):
        return issues

    seen_ids: set[str] = set()
    running_count = 0
    for idx, exp in enumerate(experiments, start=1):
        if not _is_mapping(exp):
            issues.append(f"experiment #{idx} is not an object")
            continue
        exp_id = exp.get("id")
        if not isinstance(exp_id, str) or not exp_id.strip():
            issues.append(f"experiment #{idx} missing id")
            continue
        if exp_id in seen_ids:
            issues.append(f"duplicate experiment id: {exp_id}")
        seen_ids.add(exp_id)

        exp_status = exp.get("status")
        if not isinstance(exp_status, str) or exp_status not in _VALID_EXPERIMENT_STATUS:
            issues.append(f"{exp_id}: invalid status")
            continue

        if exp_status == "running":
            running_count += 1
        if exp_status == "completed" and not (exp.get("results") or exp.get("conclusion")):
            issues.append(f"{exp_id}: completed experiment missing results/conclusion")

        if project_dir and isinstance(exp.get("results"), dict):
            artifacts = exp["results"].get("artifacts")
            if isinstance(artifacts, list):
                exp_dirname = _experiment_dirname(exp_id)
                valid_prefixes = (f"experiments/{exp_dirname}/", f"outputs/{exp_dirname}/")
                for artifact in artifacts:
                    if not isinstance(artifact, str):
                        issues.append(f"{exp_id}: non-string artifact path")
                        continue
                    if artifact.startswith("/") or artifact.startswith("../") or "/../" in artifact:
                        issues.append(f"{exp_id}: unsafe artifact path '{artifact}'")
                        continue
                    if not artifact.startswith(valid_prefixes):
                        issues.append(
                            f"{exp_id}: artifact path outside canonical dirs '{artifact}'"
                        )

    if running_count > 1:
        issues.append("more than one experiment marked as running")

    current = data.get("current_experiment")
    if isinstance(current, str) and current and current not in seen_ids:
        issues.append("current_experiment does not match any experiment id")
    return issues


def reconcile_task_plan_data(data: dict[str, Any], project_dir: Path) -> tuple[dict[str, Any], bool]:
    """Normalize and enrich plan data using workspace artifacts."""
    normalized = json.loads(json.dumps(data, ensure_ascii=False))

    experiments = normalized.get("experiments")
    if not isinstance(experiments, list):
        return normalized, False

    changed = False
    if not isinstance(normalized.get("schema_version"), int):
        normalized["schema_version"] = PLAN_SCHEMA_VERSION
        changed = True
    if normalized.get("status") not in _VALID_PLAN_STATUS:
        normalized["status"] = "in_progress"
        changed = True

    running_seen = False
    updated_experiments: list[dict[str, Any]] = []
    for idx, exp in enumerate(experiments, start=1):
        item = dict(exp) if _is_mapping(exp) else {}
        if not _is_mapping(exp):
            changed = True

        exp_id = _normalize_experiment_id(item.get("id"), idx)
        if item.get("id") != exp_id:
            item["id"] = exp_id
            changed = True
        if not isinstance(item.get("title"), str) or not item["title"].strip():
            item["title"] = exp_id
            changed = True

        status = item.get("status")
        if status not in _VALID_EXPERIMENT_STATUS:
            item["status"] = "pending"
            status = "pending"
            changed = True
        if status == "running":
            if running_seen:
                item["status"] = "pending"
                status = "pending"
                changed = True
            running_seen = True

        recovered = _recover_results(project_dir, exp_id)
        if status in {"pending", "running"} and recovered.get("metrics") is not None:
            item["status"] = "completed"
            status = "completed"
            changed = True
        merged_results = _merge_results(item.get("results"), recovered)
        if merged_results and item.get("results") != merged_results:
            item["results"] = merged_results
            changed = True

        if status == "completed" and not item.get("conclusion"):
            findings = merged_results.get("findings") if isinstance(merged_results, dict) else None
            if isinstance(findings, str) and findings.strip():
                item["conclusion"] = findings[:240]
            elif merged_results:
                item["conclusion"] = "Recovered completed experiment artifacts from workspace."
            if item.get("conclusion"):
                changed = True

        updated_experiments.append(item)

    if updated_experiments != experiments:
        normalized["experiments"] = updated_experiments
        changed = True

    all_ids = [exp.get("id") for exp in updated_experiments if _is_mapping(exp)]
    status_by_id = {
        exp.get("id"): exp.get("status")
        for exp in updated_experiments
        if _is_mapping(exp) and isinstance(exp.get("id"), str)
    }
    has_running = any(status == "running" for status in status_by_id.values())
    first_pending = next(
        (exp_id for exp_id, status in status_by_id.items() if status == "pending"),
        None,
    )
    current = normalized.get("current_experiment")
    current_status = status_by_id.get(current) if isinstance(current, str) else None
    if first_pending and (
        current not in all_ids
        or (not has_running and current_status in {"completed", "failed", "skipped"})
    ):
        normalized["current_experiment"] = first_pending
        changed = True

    return normalized, changed


def guard_task_plan_file(project_dir: Path, auto_fix: bool = True) -> dict[str, Any]:
    """Validate (and optionally auto-fix) task_plan.json under a project directory."""
    plan_path = project_dir / PLAN_FILENAME
    if not plan_path.is_file():
        return {
            "ok": True,
            "exists": False,
            "fixed": False,
            "blocking": False,
            "issues": [],
        }

    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {
            "ok": False,
            "exists": True,
            "fixed": False,
            "blocking": True,
            "issues": [f"failed to parse task_plan.json: {exc}"],
        }

    if not _is_mapping(data):
        return {
            "ok": False,
            "exists": True,
            "fixed": False,
            "blocking": True,
            "issues": ["task_plan root must be a JSON object"],
        }

    fixed = False
    if auto_fix:
        normalized, changed = reconcile_task_plan_data(data, project_dir)
        if changed:
            try:
                plan_path.write_text(
                    json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                data = normalized
                fixed = True
            except OSError as exc:
                return {
                    "ok": False,
                    "exists": True,
                    "fixed": False,
                    "blocking": True,
                    "issues": [f"failed to write normalized task_plan.json: {exc}"],
                }

    issues = lint_task_plan_data(data, project_dir=project_dir)
    return {
        "ok": len(issues) == 0,
        "exists": True,
        "fixed": fixed,
        "blocking": len(issues) > 0,
        "issues": issues,
    }
