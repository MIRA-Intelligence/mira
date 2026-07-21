"""Guardrails for task_plan.json consistency and resilience."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

PLAN_FILENAME = "task_plan.json"
PLAN_SCHEMA_VERSION = 1
PROJECT_META_PATH = Path(".mira") / "project.json"
DEFAULT_CONTRACT_VERSION = 1
STRICT_CONTRACT_VERSION = 2

_VALID_PLAN_STATUS = {"in_progress", "completed", "failed"}
_VALID_EXPERIMENT_STATUS = {"pending", "running", "completed", "failed", "skipped"}
# Interactive plan-mode (the ``plan`` block inside task_plan.json).
_VALID_PLAN_PHASES = {"questions", "draft", "approved"}
_VALID_PLAN_QUESTION_KINDS = {"single", "multi", "text"}
_EXP_ID_PATTERN = re.compile(r"(?i)^exp[-_ ]?(\d{1,4})$")
_RESEARCH_REQUIRED_COMPLETED_FIELDS = (
    "theoretical_proof",
    "isolation_test.control",
    "isolation_test.treatment",
    "isolation_test.isolated_variable",
    "post_mortem.residual_analysis",
    "post_mortem.implementation_fidelity",
    "post_mortem.five_whys",
    "evidence_refs",
)
_RESEARCH_REQUIRED_FALSIFY_FIELDS = (
    "theoretical_proof",
    "isolation_test.control",
    "isolation_test.treatment",
    "evidence_refs",
)
_ENGINEER_REQUIRED_COMPLETED_FIELDS = (
    "commit",
    "repro.script_path",
    "repro.seed",
    "repro.env",
    "tests.summary",
)
_ENGINEER_REQUIRED_FALSIFY_FIELDS = (
    "evidence_refs",
    "tests.summary",
)
_DEFAULT_REQUIRED_COMPLETED_FIELDS: tuple[str, ...] = ()
_DEFAULT_STRICT_REQUIRED_COMPLETED_FIELDS = (
    "question",
    "hypothesis",
    "method",
    "results",
    "conclusion",
)
_DEFAULT_REQUIRED_FALSIFY_FIELDS = ("evidence_refs",)
_FALSIFY_KEYWORDS = (
    "falsif",
    "reject",
    "rejected",
    "fail",
    "failed",
    "not supported",
    "不支持",
    "否定",
    "拒绝",
)
_JSON_RESULT_HINT_KEYS = {
    "metrics",
    "findings",
    "artifacts",
    "score",
    "mean_r",
    "mean_r2",
    "best_transform",
    "overall_r2",
    "overall_r",
}
_IGNORED_JSON_SCAN_DIRS = {".git", ".mira", "__pycache__", "node_modules", ".venv", "venv"}


def _normalize_profile(profile: object) -> str:
    if isinstance(profile, str):
        normalized = profile.strip().lower()
        if normalized in {"research", "engineer", "default", "team"}:
            return normalized
    return "default"


def _normalize_contract_version(contract_version: object) -> int:
    if isinstance(contract_version, int) and contract_version in {
        DEFAULT_CONTRACT_VERSION,
        STRICT_CONTRACT_VERSION,
    }:
        return contract_version
    return DEFAULT_CONTRACT_VERSION


def plan_has_final_result_output(result: object) -> bool:
    """Return whether task_plan.result contains a user-visible final deliverable."""
    if not isinstance(result, dict):
        return False
    output_path = result.get("output_path")
    output_type = result.get("output_type")
    summary = result.get("summary")
    sections = result.get("sections")
    if isinstance(output_path, str) and output_path.strip():
        return True
    if isinstance(output_type, str) and output_type.strip():
        return True
    if isinstance(summary, str) and summary.strip():
        return True
    return isinstance(sections, list) and any(
        isinstance(section, dict)
        and (
            isinstance(section.get("title"), str)
            and section.get("title").strip()
            or isinstance(section.get("content"), str)
            and section.get("content").strip()
        )
        for section in sections
    )


def _required_completed_fields_for_profile(
    profile: str, contract_version: int
) -> tuple[str, ...]:
    if contract_version < STRICT_CONTRACT_VERSION:
        return ()
    # The team profile is research-oriented (supervisor plans rigorous work, the
    # critic enforces falsifiability), so it reuses the research field contract.
    if profile in {"research", "team"}:
        return _RESEARCH_REQUIRED_COMPLETED_FIELDS
    if profile == "engineer":
        return _ENGINEER_REQUIRED_COMPLETED_FIELDS
    if profile == "default":
        return _DEFAULT_STRICT_REQUIRED_COMPLETED_FIELDS
    return ()


def _required_falsify_fields_for_profile(
    profile: str, contract_version: int
) -> tuple[str, ...]:
    if contract_version < STRICT_CONTRACT_VERSION:
        return ()
    if profile in {"research", "team"}:
        return _RESEARCH_REQUIRED_FALSIFY_FIELDS
    if profile == "engineer":
        return _ENGINEER_REQUIRED_FALSIFY_FIELDS
    if profile == "default":
        return _DEFAULT_REQUIRED_FALSIFY_FIELDS
    return ()


def get_task_plan_contract(
    *, profile: object = "default", contract_version: object = DEFAULT_CONTRACT_VERSION
) -> dict[str, Any]:
    normalized_profile = _normalize_profile(profile)
    normalized_contract_version = _normalize_contract_version(contract_version)
    return {
        "profile": normalized_profile,
        "contract_version": normalized_contract_version,
        "required_completed_fields": list(
            _required_completed_fields_for_profile(
                normalized_profile, normalized_contract_version
            )
        ),
        "required_falsify_fields": list(
            _required_falsify_fields_for_profile(
                normalized_profile, normalized_contract_version
            )
        ),
        "falsify_keywords": list(_FALSIFY_KEYWORDS),
    }


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


def _experiment_numeric_id(exp_id: object) -> int | None:
    if not isinstance(exp_id, str):
        return None
    match = _EXP_ID_PATTERN.match(exp_id.strip())
    if not match:
        return None
    return int(match.group(1))


def _next_experiment_id(used_ids: set[str], start: int) -> tuple[str, int]:
    candidate = max(1, start)
    while True:
        exp_id = f"Exp{candidate:03d}"
        if exp_id not in used_ids:
            return exp_id, candidate
        candidate += 1


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


def _load_project_profile(project_dir: Path | None) -> str:
    if project_dir is None:
        return "default"
    meta = _load_json(project_dir / PROJECT_META_PATH)
    if isinstance(meta, dict):
        profile = meta.get("agent_profile")
        if isinstance(profile, str):
            normalized = profile.strip().lower()
            if normalized in {"research", "engineer", "default", "team"}:
                return normalized
    return "default"


def _load_project_contract_version(project_dir: Path | None) -> int:
    if project_dir is None:
        return DEFAULT_CONTRACT_VERSION
    meta = _load_json(project_dir / PROJECT_META_PATH)
    if isinstance(meta, dict):
        value = meta.get("contract_version")
        if isinstance(value, int) and value in {DEFAULT_CONTRACT_VERSION, STRICT_CONTRACT_VERSION}:
            return value
    return DEFAULT_CONTRACT_VERSION


# Severe issues make the plan machine-unreadable/unwritable, escape the
# workspace, or corrupt experiment structure beyond auto-fix recovery. Only
# these should ever halt the auto-continue loop. Everything else is a minor
# content/quality issue: surfaced as a per-experiment warning marker and
# repaired opportunistically (reconcile), but never blocking the whole run.
_SEVERE_ISSUE_MARKERS = (
    "failed to parse task_plan.json",
    "failed to write normalized task_plan.json",
    "task_plan root must be a JSON object",
    "unsafe artifact path",
    ": invalid status",
    "more than one experiment marked as running",
)


def _is_severe_blocking_issue(issue: str) -> bool:
    """Return whether an issue is severe enough to block auto-continue."""
    if any(marker in issue for marker in _SEVERE_ISSUE_MARKERS):
        return True
    # Experiment-level structural corruption that survives reconcile.
    if issue.startswith("experiment #") and (
        "is not an object" in issue or "missing id" in issue
    ):
        return True
    return False


def _build_guard_result(
    *,
    ok: bool | None = None,
    exists: bool,
    fixed: bool,
    issues: list[str],
    contract_version: int = DEFAULT_CONTRACT_VERSION,
) -> dict[str, Any]:
    severe_issues = [issue for issue in issues if _is_severe_blocking_issue(issue)]
    minor_issues = [issue for issue in issues if not _is_severe_blocking_issue(issue)]
    if ok is None:
        # Default contract: only severe issues halt the loop; minor issues are
        # surfaced as per-experiment warning markers and repaired opportunistically.
        # Strict contract (opt-in): every issue blocks, enforcing full rigor.
        blocking_issues = (
            issues
            if contract_version >= STRICT_CONTRACT_VERSION
            else severe_issues
        )
        ok = len(blocking_issues) == 0
    else:
        blocking_issues = issues if not ok else []

    return {
        "ok": ok,
        "exists": exists,
        "fixed": fixed,
        "blocking": len(blocking_issues) > 0,
        "issues": issues,
        # ``repairable_issues``/``fatal_issues`` keys are kept for backward
        # compatibility (research_loop reads them); they now mean minor/severe.
        "repairable_issues": minor_issues,
        "fatal_issues": severe_issues,
        "minor_issues": minor_issues,
        "severe_issues": severe_issues,
        "blocking_issues": blocking_issues,
    }


def _is_nonempty(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (int, float, bool)):
        return True
    if isinstance(value, list):
        return any(_is_nonempty(item) for item in value)
    if isinstance(value, dict):
        return any(_is_nonempty(item) for item in value.values())
    return value is not None


def _is_guardrail_placeholder_text(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return value.strip().lower().startswith("guardrail auto-fill:")


def _get_nested(exp: dict[str, Any], dotted: str) -> tuple[bool, Any]:
    current: Any = exp
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _missing_required_fields(
    exp: dict[str, Any],
    fields: tuple[str, ...],
    *,
    allow_guardrail_placeholders: bool = True,
) -> list[str]:
    missing: list[str] = []
    for field in fields:
        exists, value = _get_nested(exp, field)
        if not exists or not _is_nonempty(value):
            missing.append(field)
            continue
        if not allow_guardrail_placeholders and _is_guardrail_placeholder_text(value):
            missing.append(field)
    return missing


_ARTIFACT_GLOB_RE = re.compile(r"[*?\[]")


def _artifact_reference_exists(project_dir: Path, artifact: str) -> bool:
    """Return whether an artifact reference resolves to at least one real file.

    Supports glob patterns (e.g. ``outputs/exp010/predictions_lovo_*_*.csv``) so
    plans that summarize many per-fold/per-seed outputs with a single wildcard
    entry are not rejected as missing. Non-glob paths keep the exact
    ``is_file`` semantics.
    """
    if _ARTIFACT_GLOB_RE.search(artifact):
        try:
            return any(match.is_file() for match in project_dir.glob(artifact))
        except (ValueError, OSError):
            return False
    return (project_dir / artifact).is_file()


def _looks_like_hypothesis_rejection(text: object) -> bool:
    if not isinstance(text, str):
        return False
    lowered = text.lower()
    return any(keyword in lowered for keyword in _FALSIFY_KEYWORDS)


def _validate_evidence_refs(
    exp_id: str, exp: dict[str, Any], project_dir: Path | None
) -> list[str]:
    issues: list[str] = []
    refs = exp.get("evidence_refs")
    if refs is None:
        return issues
    if not isinstance(refs, list):
        issues.append(f"{exp_id}: evidence_refs must be a list")
        return issues
    metrics = exp.get("results", {}).get("metrics") if isinstance(exp.get("results"), dict) else None
    for idx, ref in enumerate(refs, start=1):
        if not isinstance(ref, dict):
            issues.append(f"{exp_id}: evidence_refs[{idx}] must be an object")
            continue
        metric_key = ref.get("metric_key")
        if metric_key is not None and not isinstance(metric_key, str):
            issues.append(f"{exp_id}: evidence_refs[{idx}].metric_key must be a string")
        artifact = ref.get("artifact")
        if artifact is not None and not isinstance(artifact, str):
            issues.append(f"{exp_id}: evidence_refs[{idx}].artifact must be a string")
        if isinstance(metric_key, str) and metric_key and isinstance(metrics, dict):
            if metric_key not in metrics:
                issues.append(
                    f"{exp_id}: evidence_refs[{idx}] metric_key '{metric_key}' not found in results.metrics"
                )
        if isinstance(artifact, str) and artifact and project_dir is not None:
            if not _artifact_reference_exists(project_dir, artifact):
                issues.append(
                    f"{exp_id}: evidence_refs[{idx}] artifact '{artifact}' does not exist"
                )
    return issues


def _validate_profile_required_fields(
    exp_id: str, exp: dict[str, Any], *, profile: str, contract_version: int
) -> list[str]:
    required = _required_completed_fields_for_profile(profile, contract_version)
    missing = _missing_required_fields(
        exp,
        required,
        allow_guardrail_placeholders=contract_version < STRICT_CONTRACT_VERSION,
    )
    if missing:
        return [f"{exp_id}: {profile} profile missing required fields: {', '.join(missing)}"]
    return []


def _validate_profile_falsify_fields(
    exp_id: str, exp: dict[str, Any], *, profile: str, contract_version: int
) -> list[str]:
    if not _looks_like_hypothesis_rejection(exp.get("conclusion")):
        return []
    required = _required_falsify_fields_for_profile(profile, contract_version)
    missing = _missing_required_fields(
        exp,
        required,
        allow_guardrail_placeholders=contract_version < STRICT_CONTRACT_VERSION,
    )
    if missing:
        return [f"{exp_id}: hypothesis rejection requires fields: {', '.join(missing)}"]
    return []


def _count_numeric_leaves(value: object, depth: int = 0, max_depth: int = 4) -> tuple[int, int]:
    if depth > max_depth:
        return 0, 0
    if isinstance(value, bool):
        return 0, 1
    if isinstance(value, (int, float)):
        return 1, 0
    if isinstance(value, str) or value is None:
        return 0, 1
    if isinstance(value, list):
        numeric = 0
        other = 0
        for item in value:
            n, o = _count_numeric_leaves(item, depth + 1, max_depth=max_depth)
            numeric += n
            other += o
        return numeric, other
    if isinstance(value, dict):
        numeric = 0
        other = 0
        for item in value.values():
            n, o = _count_numeric_leaves(item, depth + 1, max_depth=max_depth)
            numeric += n
            other += o
        return numeric, other
    return 0, 1


def _looks_like_experiment_metrics(payload: object) -> bool:
    if not _is_mapping(payload):
        return False
    lowered_keys = {str(key).strip().lower() for key in payload.keys()}
    if lowered_keys.intersection(_JSON_RESULT_HINT_KEYS):
        return True
    numeric_count, other_count = _count_numeric_leaves(payload)
    return numeric_count >= 3 and numeric_count >= other_count


def _iter_project_json_paths(project_dir: Path) -> list[Path]:
    json_paths: list[Path] = []
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [name for name in dirs if name not in _IGNORED_JSON_SCAN_DIRS]
        for name in files:
            if name.lower().endswith(".json"):
                json_paths.append(Path(root) / name)
    json_paths.sort()
    return json_paths


def _iter_experiment_json_candidates(project_dir: Path, exp_id: str) -> list[Path]:
    exp_dirname = _experiment_dirname(exp_id)
    ordered: list[Path] = []
    seen: set[Path] = set()

    def _add(path: Path) -> None:
        if path in seen or not path.is_file() or path.suffix.lower() != ".json":
            return
        seen.add(path)
        ordered.append(path)

    _add(project_dir / "outputs" / exp_dirname / "results.json")
    _add(project_dir / "experiments" / exp_dirname / "results.json")
    _add(project_dir / "experiments" / exp_dirname / "metrics.json")

    for base in (project_dir / "experiments" / exp_dirname, project_dir / "outputs" / exp_dirname):
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.json")):
            _add(path)

    exp_tokens = {exp_id.strip().lower(), exp_dirname.lower()}
    for path in _iter_project_json_paths(project_dir):
        rel_path = path.relative_to(project_dir).as_posix().lower()
        if any(token and token in rel_path for token in exp_tokens):
            _add(path)
    return ordered


def _recover_from_git_commit(project_dir: Path, exp_id: str) -> tuple[str | None, list[str]]:
    """Recover experiment evidence from git commit history when artifacts are non-standard."""
    try:
        log = subprocess.run(
            [
                "git",
                "-C",
                str(project_dir),
                "log",
                "--max-count",
                "1",
                "--regexp-ignore-case",
                "--grep",
                rf"^{re.escape(exp_id)}\b",
                "--pretty=format:%H%x09%s",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None, []
    line = (log.stdout or "").strip()
    if not line:
        return None, []
    parts = line.split("\t", 1)
    commit_hash = parts[0].strip() if parts else ""
    subject = parts[1].strip() if len(parts) > 1 else ""
    if not commit_hash:
        return None, []

    try:
        changed = subprocess.run(
            [
                "git",
                "-C",
                str(project_dir),
                "show",
                "--name-only",
                "--pretty=format:",
                commit_hash,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return subject or None, []

    artifacts: list[str] = []
    seen: set[str] = set()
    for rel in (changed.stdout or "").splitlines():
        candidate = rel.strip()
        if not candidate:
            continue
        if candidate.startswith("/") or candidate.startswith("../") or "/../" in candidate:
            continue
        lowered = candidate.lower()
        if lowered in {"task_plan.json", ".gitignore"}:
            continue
        if lowered.startswith(".mira/") or lowered.startswith(".git/"):
            continue
        full = project_dir / candidate
        if not full.is_file():
            continue
        normalized = full.relative_to(project_dir).as_posix()
        if normalized in seen:
            continue
        seen.add(normalized)
        artifacts.append(normalized)
    return subject or None, artifacts


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
    inferred_artifacts: set[str] = set()

    for json_path in _iter_experiment_json_candidates(project_dir, exp_id):
        payload = _load_json(json_path)
        if not _looks_like_experiment_metrics(payload):
            continue
        rel_path = json_path.relative_to(project_dir).as_posix()
        inferred_artifacts.add(rel_path)

        if _is_mapping(payload) and any(key in payload for key in ("metrics", "findings", "artifacts")):
            if "metrics" not in recovered and payload.get("metrics") is not None:
                recovered["metrics"] = payload.get("metrics")
            if "findings" not in recovered and isinstance(payload.get("findings"), str):
                recovered["findings"] = payload.get("findings")
            if "artifacts" not in recovered and isinstance(payload.get("artifacts"), list):
                recovered["artifacts"] = payload.get("artifacts")
            continue

        if "metrics" not in recovered and payload is not None:
            recovered["metrics"] = payload

    commit_findings, commit_artifacts = _recover_from_git_commit(project_dir, exp_id)
    if "findings" not in recovered and isinstance(commit_findings, str) and commit_findings.strip():
        recovered["findings"] = commit_findings.strip()
    inferred_artifacts.update(commit_artifacts)

    if "findings" not in recovered:
        exp_dir = project_dir / "experiments" / exp_dirname
        if exp_dir.is_dir():
            md_files = sorted(exp_dir.glob("*.md"))
            if md_files:
                summary = _load_text(md_files[0])
                if summary:
                    recovered["findings"] = summary

    artifacts = set(_collect_artifacts(project_dir, exp_id))
    artifacts.update(inferred_artifacts)
    if artifacts:
        recovered["artifacts"] = sorted(artifacts)

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


def _build_evidence_refs_from_artifacts(artifacts: list[str]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, str):
            continue
        normalized = artifact.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        refs.append({"artifact": normalized})
    return refs


def _auto_fill_research_contract_fields(exp: dict[str, Any]) -> bool:
    """Populate strict research contract placeholders for auto-recovered experiments."""
    changed = False
    method = exp.get("method") if isinstance(exp.get("method"), str) else ""
    hypothesis = exp.get("hypothesis") if isinstance(exp.get("hypothesis"), str) else ""
    question = exp.get("question") if isinstance(exp.get("question"), str) else ""

    if not _is_nonempty(exp.get("theoretical_proof")):
        exp["theoretical_proof"] = (
            "Guardrail auto-fill: experiment completion was recovered from workspace artifacts. "
            "Review and replace with explicit theoretical derivation."
        )
        changed = True

    isolation_test = exp.get("isolation_test")
    if not isinstance(isolation_test, dict):
        isolation_test = {}
        exp["isolation_test"] = isolation_test
        changed = True
    if not _is_nonempty(isolation_test.get("control")):
        isolation_test["control"] = (
            "Baseline defined by prior plan/previous experiment outputs."
        )
        changed = True
    if not _is_nonempty(isolation_test.get("treatment")):
        isolation_test["treatment"] = method or "Current experiment implementation."
        changed = True
    if not _is_nonempty(isolation_test.get("isolated_variable")):
        isolation_test["isolated_variable"] = hypothesis or question or "Model/data configuration"
        changed = True

    post_mortem = exp.get("post_mortem")
    if not isinstance(post_mortem, dict):
        post_mortem = {}
        exp["post_mortem"] = post_mortem
        changed = True
    if not _is_nonempty(post_mortem.get("residual_analysis")):
        post_mortem["residual_analysis"] = (
            "Guardrail auto-fill: residual analysis unavailable in structured form; inspect artifacts."
        )
        changed = True
    if not _is_nonempty(post_mortem.get("implementation_fidelity")):
        post_mortem["implementation_fidelity"] = (
            "Guardrail auto-fill: execution artifacts detected and marked as completed."
        )
        changed = True
    if not _is_nonempty(post_mortem.get("five_whys")):
        post_mortem["five_whys"] = (
            "Guardrail auto-fill: root-cause chain not provided by agent output."
        )
        changed = True

    refs = exp.get("evidence_refs")
    if not isinstance(refs, list) or not refs:
        artifacts = []
        if isinstance(exp.get("results"), dict) and isinstance(exp["results"].get("artifacts"), list):
            artifacts = [item for item in exp["results"]["artifacts"] if isinstance(item, str)]
        generated_refs = _build_evidence_refs_from_artifacts(artifacts)
        if not generated_refs:
            generated_refs = [{"artifact": "task_plan.json"}]
        exp["evidence_refs"] = generated_refs
        changed = True
    return changed


def _auto_fill_contract_fields(
    exp: dict[str, Any], *, profile: str, contract_version: int
) -> bool:
    if profile in {"research", "team"} and contract_version >= STRICT_CONTRACT_VERSION:
        return _auto_fill_research_contract_fields(exp)
    return False


def lint_plan_block(plan: object) -> list[str]:
    """Validate the interactive plan-mode ``plan`` block, if present.

    The ``plan`` block is written by the ``set_plan`` tool and read by the UI
    to render the interactive planning stage. It is optional; absence yields no
    issues. Unknown keys are tolerated so the schema can evolve.
    """
    issues: list[str] = []
    if plan is None:
        return issues
    if not _is_mapping(plan):
        return ["plan must be a JSON object"]

    phase = plan.get("phase")
    if phase is not None and phase not in _VALID_PLAN_PHASES:
        issues.append(f"plan.phase invalid: {phase!r}")

    questions = plan.get("questions")
    if questions is not None:
        if not isinstance(questions, list):
            issues.append("plan.questions must be a list")
        else:
            seen_ids: set[str] = set()
            for idx, question in enumerate(questions, start=1):
                if not _is_mapping(question):
                    issues.append(f"plan question #{idx} is not an object")
                    continue
                qid = question.get("id")
                if not isinstance(qid, str) or not qid.strip():
                    issues.append(f"plan question #{idx} missing id")
                elif qid in seen_ids:
                    issues.append(f"duplicate plan question id: {qid}")
                else:
                    seen_ids.add(qid)
                prompt = question.get("prompt")
                if not isinstance(prompt, str) or not prompt.strip():
                    issues.append(f"plan question {qid or idx} missing prompt")
                kind = question.get("kind")
                if kind not in _VALID_PLAN_QUESTION_KINDS:
                    issues.append(f"plan question {qid or idx} invalid kind: {kind!r}")
                elif kind in {"single", "multi"}:
                    options = question.get("options")
                    if not isinstance(options, list) or not options:
                        issues.append(
                            f"plan question {qid or idx} requires options for kind '{kind}'"
                        )

    draft = plan.get("draft")
    if draft is not None and not _is_mapping(draft):
        issues.append("plan.draft must be a JSON object")
    return issues


def lint_task_plan_data(
    data: object,
    project_dir: Path | None = None,
    profile: str | None = None,
    contract_version: int | None = None,
) -> list[str]:
    """Return structural issues found in a task plan object."""
    issues: list[str] = []
    if not _is_mapping(data):
        return ["task_plan root must be a JSON object"]
    effective_profile = _normalize_profile(profile or _load_project_profile(project_dir))
    if contract_version is not None:
        effective_contract_version = _normalize_contract_version(contract_version)
    else:
        effective_contract_version = _normalize_contract_version(
            _load_project_contract_version(project_dir)
        )

    issues.extend(lint_plan_block(data.get("plan")))

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
        if exp_status == "completed":
            issues.extend(
                _validate_profile_required_fields(
                    exp_id,
                    exp,
                    profile=effective_profile,
                    contract_version=effective_contract_version,
                )
            )
            issues.extend(
                _validate_profile_falsify_fields(
                    exp_id,
                    exp,
                    profile=effective_profile,
                    contract_version=effective_contract_version,
                )
            )
            issues.extend(_validate_evidence_refs(exp_id, exp, project_dir))

        if project_dir and isinstance(exp.get("results"), dict):
            artifacts = exp["results"].get("artifacts")
            if isinstance(artifacts, list):
                for artifact in artifacts:
                    if not isinstance(artifact, str):
                        issues.append(f"{exp_id}: non-string artifact path")
                        continue
                    if artifact.startswith("/") or artifact.startswith("../") or "/../" in artifact:
                        issues.append(f"{exp_id}: unsafe artifact path '{artifact}'")
                        continue
                    if not _artifact_reference_exists(project_dir, artifact):
                        issues.append(f"{exp_id}: artifact path does not exist '{artifact}'")

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
    effective_profile = _normalize_profile(_load_project_profile(project_dir))
    effective_contract_version = _normalize_contract_version(
        _load_project_contract_version(project_dir)
    )

    running_seen = False
    updated_experiments: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    max_numeric_id = 0
    for idx, exp in enumerate(experiments, start=1):
        item = dict(exp) if _is_mapping(exp) else {}
        if not _is_mapping(exp):
            changed = True

        exp_id = _normalize_experiment_id(item.get("id"), idx)
        numeric_id = _experiment_numeric_id(exp_id)
        if numeric_id is not None:
            max_numeric_id = max(max_numeric_id, numeric_id)
        if exp_id in used_ids:
            exp_id, max_numeric_id = _next_experiment_id(
                used_ids,
                max(max_numeric_id + 1, idx),
            )
            item["id"] = exp_id
            changed = True
        used_ids.add(exp_id)
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
        has_recoverable_evidence = recovered.get("metrics") is not None or bool(
            recovered.get("artifacts")
        )
        strict_requires_structured_completion = (
            effective_contract_version >= STRICT_CONTRACT_VERSION
            and recovered.get("metrics") is None
        )
        if status in {"pending", "running"} and has_recoverable_evidence and not strict_requires_structured_completion:
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
        if (
            status == "completed"
            and effective_contract_version < STRICT_CONTRACT_VERSION
            and _auto_fill_contract_fields(
                item,
                profile=effective_profile,
                contract_version=effective_contract_version,
            )
        ):
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

    if normalized.get("status") == "completed" and (
        has_running
        or first_pending is not None
        or not plan_has_final_result_output(normalized.get("result"))
    ):
        normalized["status"] = "in_progress"
        changed = True

    return normalized, changed


def _experiment_scoped_warnings(
    minor_issues: list[str], valid_ids: set[str]
) -> dict[str, list[str]]:
    """Group minor issues under their owning experiment id (``ExpNNN: detail``)."""
    warnings_by_id: dict[str, list[str]] = {}
    for issue in minor_issues:
        exp_id, sep, detail = issue.partition(": ")
        if not sep:
            continue
        exp_id = exp_id.strip()
        if exp_id not in valid_ids:
            continue
        warnings_by_id.setdefault(exp_id, []).append(detail.strip())
    return warnings_by_id


def _apply_guard_warnings(data: dict[str, Any], minor_issues: list[str]) -> bool:
    """Write minor (non-blocking) issues onto their experiment as ``guard_warnings``.

    The UI reads this list to render a warning marker after the experiment
    title. Returns whether the plan payload changed.
    """
    experiments = data.get("experiments")
    if not isinstance(experiments, list):
        return False
    valid_ids = {
        exp.get("id")
        for exp in experiments
        if _is_mapping(exp) and isinstance(exp.get("id"), str)
    }
    warnings_by_id = _experiment_scoped_warnings(minor_issues, valid_ids)
    changed = False
    for exp in experiments:
        if not _is_mapping(exp):
            continue
        new_warnings = sorted(warnings_by_id.get(exp.get("id"), []))
        if new_warnings:
            if exp.get("guard_warnings") != new_warnings:
                exp["guard_warnings"] = new_warnings
                changed = True
        elif "guard_warnings" in exp:
            del exp["guard_warnings"]
            changed = True
    return changed


def guard_task_plan_file(
    project_dir: Path, auto_fix: bool = True, profile: str | None = None
) -> dict[str, Any]:
    """Validate (and optionally auto-fix) task_plan.json under a project directory."""
    plan_path = project_dir / PLAN_FILENAME
    if not plan_path.is_file():
        return _build_guard_result(
            ok=True,
            exists=False,
            fixed=False,
            issues=[],
            contract_version=_load_project_contract_version(project_dir),
        )

    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return _build_guard_result(
            ok=False,
            exists=True,
            fixed=False,
            issues=[f"failed to parse task_plan.json: {exc}"],
            contract_version=_load_project_contract_version(project_dir),
        )

    if not _is_mapping(data):
        return _build_guard_result(
            ok=False,
            exists=True,
            fixed=False,
            issues=["task_plan root must be a JSON object"],
            contract_version=_load_project_contract_version(project_dir),
        )

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
                return _build_guard_result(
                    ok=False,
                    exists=True,
                    fixed=False,
                    issues=[f"failed to write normalized task_plan.json: {exc}"],
                    contract_version=_load_project_contract_version(project_dir),
                )

    issues = lint_task_plan_data(data, project_dir=project_dir, profile=profile)
    if auto_fix:
        minor_issues = [i for i in issues if not _is_severe_blocking_issue(i)]
        if _apply_guard_warnings(data, minor_issues):
            try:
                plan_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                fixed = True
            except OSError:
                pass
    return _build_guard_result(
        exists=True,
        fixed=fixed,
        issues=issues,
        contract_version=_load_project_contract_version(project_dir),
    )
