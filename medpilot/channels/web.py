"""Web channel – exposes a WebSocket + HTTP API for browser/Electron clients."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from aiohttp import web
from loguru import logger

from medpilot import __version__
from medpilot.agent.skill_plugins import SkillPluginError, SkillPluginManager
from medpilot.bus.events import OutboundMessage
from medpilot.bus.queue import MessageBus
from medpilot.channels.base import BaseChannel
from medpilot.config import loader as config_loader
from medpilot.config.schema import WebChannelConfig
from medpilot.session.manager import SessionManager
from medpilot.task_plan.guardrails import (
    get_task_plan_contract,
    guard_task_plan_file,
    reconcile_task_plan_data,
)

PLAN_FILENAME = "task_plan.json"
PROJECT_DIR_PREFIX = "PRJ"
PROJECT_META_DIRNAME = ".medpilot"
PROJECT_META_FILENAME = "project.json"
PROJECT_META_SCHEMA_VERSION = 1
PROJECT_META_DEFAULT_RUN_MODE = "auto"
PROJECT_META_DEFAULT_AGENT_PROFILE = "default"
PROJECT_META_DEFAULT_CONTRACT_VERSION = 1
PROJECT_META_STRICT_CONTRACT_VERSION = 2
_ASSETS_DIR = Path(__file__).parent / "web_assets"
_PROJECT_AUDIT_REL_PATH = Path(".medpilot") / "logs" / "actions.jsonl"
_GLOBAL_AUDIT_FILENAME = "project_actions.jsonl"
_PROJECT_EXPERIMENT_SNAPSHOT_REL_DIR = Path(".medpilot") / "snapshots" / "experiments"
_RECOVERED_CONCLUSION_PLACEHOLDER = "Recovered completed experiment artifacts from workspace."
_API_CONTRACT_VERSION = "v1"


def _load_ui_instructions() -> str:
    """Load AGENTS_UI.md + SKILL_UI.md and return as a single system-prompt block."""
    parts: list[str] = []
    for name in ("AGENTS_UI.md", "SKILL_UI.md"):
        fp = _ASSETS_DIR / name
        if fp.is_file():
            parts.append(fp.read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(parts)


def _normalize_run_mode(value: Any) -> str:
    """Normalize UI run mode with a conservative fallback."""
    if isinstance(value, str):
        mode = value.strip().lower()
        if mode in {"manual", "auto"}:
            return mode
    return "manual"


def _normalize_agent_profile(value: Any) -> str:
    """Normalize UI agent profile with a conservative fallback."""
    if isinstance(value, str):
        profile = value.strip().lower()
        if profile in {"engineer", "default", "research"}:
            return profile
    return "default"


def _normalize_contract_version(value: Any) -> int:
    """Normalize project-level contract version with safe fallback."""
    if isinstance(value, int):
        if value in {PROJECT_META_DEFAULT_CONTRACT_VERSION, PROJECT_META_STRICT_CONTRACT_VERSION}:
            return value
    return PROJECT_META_DEFAULT_CONTRACT_VERSION


def _normalize_automation_policy(value: Any) -> dict[str, Any] | None:
    """Normalize auto-stop policy payload from UI/project meta."""
    if not isinstance(value, dict):
        return None

    logic_raw = value.get("logic")
    logic = "AND"
    if isinstance(logic_raw, str) and logic_raw.strip().upper() in {"AND", "OR"}:
        logic = logic_raw.strip().upper()

    goals: list[dict[str, Any]] = []
    raw_goals = value.get("goals")
    if isinstance(raw_goals, list):
        for item in raw_goals:
            if not isinstance(item, dict):
                continue
            metric = item.get("metric")
            operator = item.get("operator")
            raw_value = item.get("value")
            if not isinstance(metric, str) or not metric.strip():
                continue
            if not isinstance(operator, str) or operator not in {">", ">=", "<", "<=", "=="}:
                continue
            try:
                numeric = float(raw_value)
            except (TypeError, ValueError):
                continue
            goals.append({
                "metric": metric.strip(),
                "operator": operator,
                "value": numeric,
            })

    max_experiments = value.get("maxExperiments")
    if not isinstance(max_experiments, int) or max_experiments <= 0:
        max_experiments = None

    max_tokens = value.get("maxTokens")
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        max_tokens = None

    if not goals and max_experiments is None and max_tokens is None:
        return None

    normalized: dict[str, Any] = {
        "logic": logic,
        "goals": goals,
    }
    if max_experiments is not None:
        normalized["maxExperiments"] = max_experiments
    if max_tokens is not None:
        normalized["maxTokens"] = max_tokens
    return normalized


def _stringify_history_content(content: Any) -> str:
    """Flatten session content into a UI-friendly text payload."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif item.get("type") == "image_url":
                parts.append("[image]")
        return "\n".join(part for part in parts if part).strip()
    if content is None:
        return ""
    if isinstance(content, (dict, list)):
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def _format_tool_call(tool_call: dict[str, Any]) -> str:
    """Render a tool call in the same compact form shown in logs."""
    fn = tool_call.get("function") if isinstance(tool_call, dict) else None
    if isinstance(fn, dict):
        name = fn.get("name") or "tool"
        args = fn.get("arguments")
    else:
        name = tool_call.get("name") or "tool"
        args = tool_call.get("arguments")

    if isinstance(args, str):
        args_str = args.strip()
    elif args is None:
        args_str = ""
    else:
        args_str = json.dumps(args, ensure_ascii=False)

    return f"{name}({args_str})" if args_str else f"{name}()"


def _load_json_file(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _extract_plan_experiment_ids(project_dir: Path) -> list[str]:
    """Load task_plan experiment ids in order, best effort."""
    payload = _load_json_file(project_dir / PLAN_FILENAME)
    if not isinstance(payload, dict):
        return []
    experiments = payload.get("experiments")
    if not isinstance(experiments, list):
        return []
    ids: list[str] = []
    for item in experiments:
        if isinstance(item, dict):
            exp_id = item.get("id")
            if isinstance(exp_id, str) and exp_id.strip():
                ids.append(exp_id.strip())
            else:
                ids.append("")
        else:
            ids.append("")
    return ids


def _detect_guard_id_reassignments(
    before_ids: list[str],
    after_ids: list[str],
) -> list[tuple[int, str, str]]:
    """Return 1-based index id replacements made by guardrails."""
    reassignments: list[tuple[int, str, str]] = []
    for idx, (before_id, after_id) in enumerate(zip(before_ids, after_ids), start=1):
        if before_id and after_id and before_id != after_id:
            reassignments.append((idx, before_id, after_id))
    return reassignments


def _build_task_plan_guard_notice(
    reassignments: list[tuple[int, str, str]],
) -> str | None:
    """Build an LLM-facing notice about guardrail id corrections."""
    if not reassignments:
        return None
    lines = [
        "Task-plan guardrails auto-corrected duplicate/invalid experiment IDs before this turn.",
        "Use the new IDs as canonical and do not refer to retired IDs.",
        "ID remapping:",
    ]
    for idx, old_id, new_id in reassignments[:8]:
        lines.append(f"- item #{idx}: {old_id} -> {new_id}")
    return "\n".join(lines)


def _snapshot_from_experiment(exp: dict[str, Any], *, source: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "captured_at": f"{datetime.utcnow().isoformat()}Z",
        "source": source,
    }
    for key in (
        "title",
        "question",
        "hypothesis",
        "prediction",
        "method",
        "results",
        "conclusion",
        "next",
        "commit",
        "theoretical_proof",
        "isolation_test",
        "post_mortem",
        "evidence_refs",
    ):
        if key in exp:
            payload[key] = exp.get(key)
    return payload


def _is_snapshot_candidate(exp: dict[str, Any]) -> bool:
    if exp.get("status") != "completed":
        return False
    results = exp.get("results")
    findings = results.get("findings") if isinstance(results, dict) else None
    conclusion = exp.get("conclusion")
    has_findings = isinstance(findings, str) and bool(findings.strip())
    has_conclusion = (
        isinstance(conclusion, str)
        and bool(conclusion.strip())
        and conclusion.strip() != _RECOVERED_CONCLUSION_PLACEHOLDER
    )
    if has_findings or has_conclusion:
        return True

    has_metrics = (
        isinstance(results, dict)
        and isinstance(results.get("metrics"), dict)
        and bool(results.get("metrics"))
    )
    has_artifacts = (
        isinstance(results, dict)
        and isinstance(results.get("artifacts"), list)
        and bool(results.get("artifacts"))
    )
    return has_metrics and has_artifacts and not (
        isinstance(conclusion, str)
        and conclusion.strip() == _RECOVERED_CONCLUSION_PLACEHOLDER
    )


def _collect_output_artifacts(project_dir: Path, exp_id: str) -> list[str]:
    output_dir = project_dir / "outputs" / exp_id.lower()
    if not output_dir.is_dir():
        return []
    return sorted(
        path.relative_to(project_dir).as_posix()
        for path in output_dir.rglob("*")
        if path.is_file()
    )


def _latest_experiment_commit(project_dir: Path, exp_id: str) -> str | None:
    if not (project_dir / ".git").is_dir():
        return None
    try:
        result = subprocess.run(
            ["git", "log", "--format=%H", "--grep", exp_id, "-i", "-n", "1"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    commit = result.stdout.strip().splitlines()
    if not commit:
        return None
    return commit[0][:7]


def _safe_upload_name(filename: str) -> str:
    """Normalize incoming filenames to a basename-only safe value."""
    return Path(filename).name.strip().replace("\x00", "")


def _next_available_path(base_dir: Path, filename: str) -> Path:
    """Return a non-colliding destination path inside *base_dir*."""
    candidate = base_dir / filename
    if not candidate.exists():
        return candidate

    stem = Path(filename).stem or "file"
    suffix = Path(filename).suffix
    idx = 1
    while True:
        alt = base_dir / f"{stem}_{idx}{suffix}"
        if not alt.exists():
            return alt
        idx += 1


def _next_available_dir(base_dir: Path, dirname: str) -> Path:
    """Return a non-colliding directory path inside *base_dir*."""
    candidate = base_dir / dirname
    if not candidate.exists():
        return candidate

    base_name = dirname or "archive"
    idx = 1
    while True:
        alt = base_dir / f"{base_name}_{idx}"
        if not alt.exists():
            return alt
        idx += 1


def _resolve_zip_member_path(extract_root: Path, member_name: str) -> Path | None:
    """Resolve one zip member path safely under *extract_root*."""
    normalized = member_name.replace("\\", "/")
    raw = Path(normalized)
    if raw.is_absolute():
        return None

    safe_parts: list[str] = []
    for part in raw.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            return None
        safe_parts.append(part)
    if not safe_parts:
        return None

    root_resolved = extract_root.resolve()
    candidate = (extract_root / Path(*safe_parts)).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        return None
    return candidate


def _extract_zip_into_references(
    archive_path: Path,
    references_dir: Path,
    project_dir: Path,
) -> list[dict[str, Any]]:
    """Extract a ZIP archive into references/<zip_stem>/ with traversal checks."""
    stem = Path(archive_path.name).stem.strip() or "archive"
    extract_root = _next_available_dir(references_dir, stem)
    extracted: list[dict[str, Any]] = []
    archive_rel = archive_path.relative_to(project_dir).as_posix()

    try:
        with zipfile.ZipFile(archive_path) as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                target = _resolve_zip_member_path(extract_root, member.filename)
                if target is None:
                    raise ValueError(f"unsafe zip entry: {member.filename}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member, "r") as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                extracted.append({
                    "archive": archive_rel,
                    "path": target.relative_to(project_dir).as_posix(),
                    "size": target.stat().st_size,
                })
    except zipfile.BadZipFile as exc:
        raise ValueError("invalid zip archive") from exc

    return extracted


def _merge_recovered_results(existing: Any, recovered_metrics: Any, artifacts: list[str]) -> dict[str, Any]:
    results = dict(existing) if isinstance(existing, dict) else {}

    if recovered_metrics is not None and "metrics" not in results:
        if isinstance(recovered_metrics, dict) and any(
            key in recovered_metrics for key in ("metrics", "findings", "artifacts")
        ):
            for key, value in recovered_metrics.items():
                results.setdefault(key, value)
        else:
            results["metrics"] = recovered_metrics

    existing_artifacts = results.get("artifacts")
    artifact_list = list(existing_artifacts) if isinstance(existing_artifacts, list) else []
    merged_artifacts = sorted({*artifact_list, *artifacts})
    if merged_artifacts:
        results["artifacts"] = merged_artifacts

    if not results.get("findings") and recovered_metrics is not None:
        results["findings"] = "Recovered experiment output from existing workspace artifacts."

    return results


class WebChannel(BaseChannel):
    """WebSocket + REST channel for frontend clients."""

    name = "web"

    def __init__(
        self,
        config: WebChannelConfig,
        bus: MessageBus,
        workspace: Path | None = None,
        restrict_to_workspace: bool = True,
    ):
        super().__init__(config, bus)
        self.config: WebChannelConfig = config
        self.workspace: Path | None = workspace
        self.restrict_to_workspace: bool = restrict_to_workspace
        default_root = workspace or Path("~/.medpilot/workspace")
        self.projects_root: Path = default_root.expanduser().resolve()
        self._boot_ts: float = time.monotonic()
        self._ui_instructions: str = _load_ui_instructions()
        self._clients: dict[str, web.WebSocketResponse] = {}
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._migrate_global_to_project()

    @staticmethod
    def _preview(value: Any, *, limit: int = 300) -> str:
        """Render a compact, log-friendly preview for arbitrary values."""
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False)
            except TypeError:
                text = str(value)
        if len(text) <= limit:
            return text
        return text[:limit] + "...(truncated)"

    @staticmethod
    def _sanitize_details(details: dict[str, Any] | None) -> dict[str, Any]:
        """Keep audit details JSON-serializable and compact."""
        if not details:
            return {}
        safe: dict[str, Any] = {}
        for key, value in details.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                safe[key] = value if not isinstance(value, str) else WebChannel._preview(value, limit=500)
            else:
                safe[key] = WebChannel._preview(value, limit=500)
        return safe

    @staticmethod
    def _append_jsonl(path: Path, entry: dict[str, Any]) -> None:
        """Append one JSON line to path, creating parent dirs as needed."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _audit(
        self,
        *,
        source: str,
        action: str,
        session_id: str | None = None,
        project_dir: Path | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Write action audit logs to global and per-project log streams."""
        entry: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "action": action,
            "session_id": session_id,
            "details": self._sanitize_details(details),
        }
        if project_dir is not None:
            entry["project_dir"] = str(project_dir)
        elif session_id:
            entry["project_dir"] = str(self.projects_root / session_id)

        try:
            global_log = self.projects_root / "logs" / _GLOBAL_AUDIT_FILENAME
            self._append_jsonl(global_log, entry)
        except OSError as exc:
            logger.warning("Failed to append global audit log: {}", exc)

        target = project_dir
        if target is None and session_id:
            target = self.projects_root / session_id
        if target and target.is_dir():
            try:
                self._append_jsonl(target / _PROJECT_AUDIT_REL_PATH, entry)
            except OSError as exc:
                logger.warning("Failed to append project audit log for {}: {}", session_id, exc)

    # ── migration ──────────────────────────────────────────────────

    def _migrate_global_to_project(self) -> None:
        """One-time migration: move global sessions/memory into per-project dirs.

        Scans workspace_root/sessions/ for files named web_PRJ-XXXX.jsonl and
        moves them into PRJ-XXXX/sessions/.  Similarly moves global memory/ into
        the first project that exists (as a best-effort fallback).
        """
        root = self.projects_root
        global_sessions = root / "sessions"
        global_memory = root / "memory"

        if global_sessions.is_dir():
            for f in list(global_sessions.iterdir()):
                if not f.name.endswith(".jsonl"):
                    continue
                stem = f.stem  # e.g. "web_PRJ-0001"
                project_id = stem.replace("web_", "", 1)  # "PRJ-0001"
                proj_dir = root / project_id
                if not proj_dir.is_dir():
                    continue
                dest_dir = proj_dir / "sessions"
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / f.name
                if not dest.exists():
                    try:
                        shutil.move(str(f), str(dest))
                        logger.info("Migrated session {} → {}", f.name, dest)
                    except OSError as e:
                        logger.warning("Failed to migrate session {}: {}", f.name, e)
            if not any(global_sessions.iterdir()):
                try:
                    global_sessions.rmdir()
                except OSError:
                    pass

        if global_memory.is_dir():
            projects = [
                d for d in sorted(root.iterdir())
                if d.is_dir() and d.name.startswith("PRJ-")
            ]
            if len(projects) == 1:
                dest_dir = projects[0] / "memory"
                if not dest_dir.exists():
                    try:
                        shutil.move(str(global_memory), str(dest_dir))
                        logger.info("Migrated global memory → {}", dest_dir)
                    except OSError as e:
                        logger.warning("Failed to migrate memory: {}", e)
            elif not projects:
                pass
            else:
                logger.info(
                    "Multiple projects exist; skipping global memory migration. "
                    "Manually move {} into the correct project.",
                    global_memory,
                )

    # ── lifecycle ────────────────────────────────────────────────────

    def _kill_stale_listener(self) -> None:
        """Kill any leftover process occupying our port before binding."""
        import os
        import signal
        import subprocess

        my_pid = os.getpid()
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{self.config.port}"],
                capture_output=True, text=True, timeout=5,
            )
            pids = {
                int(p) for p in result.stdout.split() if p.strip()
            } - {my_pid}
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
            return

        for pid in pids:
            try:
                logger.warning("Killing stale process {} on port {}", pid, self.config.port)
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

        if pids:
            import time
            time.sleep(0.5)

    async def start(self) -> None:
        self._kill_stale_listener()

        self._app = web.Application(middlewares=[self._cors_middleware])
        self._app.router.add_get("/ws", self._ws_handler)
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/version", self._handle_version)
        self._app.router.add_get("/api/health", self._handle_health)
        self._app.router.add_get("/api/version", self._handle_version)
        self._app.router.add_get("/api/status", self._handle_status)
        self._app.router.add_get("/api/sessions", self._handle_sessions)
        self._app.router.add_get("/api/sessions/{session_id}/history", self._handle_history)
        self._app.router.add_get("/api/plan", self._handle_plan)
        self._app.router.add_get("/api/plan/contract", self._handle_plan_contract)
        self._app.router.add_get("/api/plan/lint", self._handle_plan_lint)
        self._app.router.add_post("/api/config", self._handle_config)
        self._app.router.add_get("/api/projects", self._handle_list_projects)
        self._app.router.add_post("/api/data-path/validate", self._handle_validate_data_path)
        self._app.router.add_patch("/api/projects/{session_id}/meta", self._handle_project_meta)
        self._app.router.add_delete("/api/projects", self._handle_delete_project)
        self._app.router.add_post("/api/projects/{session_id}/files", self._handle_upload_project_files)
        self._app.router.add_get("/api/projects/{session_id}/artifacts", self._handle_project_artifact)
        self._app.router.add_get("/api/projects/{session_id}/skill-plugins", self._handle_skill_plugins_list)
        self._app.router.add_post("/api/projects/{session_id}/skill-plugins/install", self._handle_skill_plugins_install)
        self._app.router.add_post("/api/projects/{session_id}/skill-plugins/state", self._handle_skill_plugins_state)
        self._app.router.add_delete("/api/projects/{session_id}/skill-plugins/{plugin_id}", self._handle_skill_plugins_uninstall)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner, self.config.host, self.config.port,
            reuse_address=True,
        )
        await self._site.start()
        self._running = True
        logger.info(
            "Web channel listening on {}:{}",
            self.config.host,
            self.config.port,
        )

        # Keep the channel alive until stopped
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        self._running = False

        for sid, ws in list(self._clients.items()):
            await ws.close()
        self._clients.clear()

        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        logger.info("Web channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        metadata = msg.metadata or {}
        if metadata.get("_audit_only"):
            action = metadata.get("_audit_event")
            details = metadata.get("_audit_details")
            if isinstance(action, str) and action:
                self._audit(
                    source="agent",
                    action=action,
                    session_id=msg.chat_id,
                    details=details if isinstance(details, dict) else {},
                )
            return

        ws = self._clients.get(msg.chat_id)
        is_progress = metadata.get("_progress", False)
        msg_type = "progress" if is_progress else "response"
        common_details = {
            "type": msg_type,
            "tool_hint": bool(metadata.get("_tool_hint", False)),
            "content_preview": self._preview(msg.content),
        }
        if msg.chat_id:
            project_dir = self.projects_root / msg.chat_id
            if project_dir.is_dir():
                SessionManager(project_dir).append_ui_event(
                    key=f"web:{msg.chat_id}",
                    role="assistant",
                    content=msg.content,
                    msg_type=msg_type,
                    metadata=metadata,
                )
        if ws is None or ws.closed:
            self._audit(
                source="agent",
                action="ws_outbound_dropped",
                session_id=msg.chat_id,
                details={**common_details, "reason": "no_active_client"},
            )
            logger.debug("No active WebSocket for chat_id={}", msg.chat_id)
            return

        payload = {
            "type": msg_type,
            "session_id": msg.chat_id,
            "content": msg.content,
            "media": msg.media,
            "metadata": metadata,
        }

        try:
            await ws.send_json(payload)
            self._audit(
                source="agent",
                action="ws_outbound_sent",
                session_id=msg.chat_id,
                details=common_details,
            )
        except Exception as e:
            self._audit(
                source="agent",
                action="ws_outbound_failed",
                session_id=msg.chat_id,
                details={**common_details, "error": self._preview(str(e), limit=400)},
            )
            logger.warning("Failed to send to {}: {}", msg.chat_id, e)

    def _reconcile_plan_data(self, project_dir: Path, data: dict[str, Any]) -> bool:
        normalized, changed = reconcile_task_plan_data(data, project_dir)
        if changed:
            data.clear()
            data.update(normalized)
        return changed

    @staticmethod
    def _snapshot_filename(exp_id: str) -> str:
        safe = "".join(ch for ch in exp_id.strip() if ch.isalnum() or ch in {"-", "_"})
        return safe or "experiment"

    def _experiment_snapshot_path(self, project_dir: Path, exp_id: str) -> Path:
        return (
            project_dir
            / _PROJECT_EXPERIMENT_SNAPSHOT_REL_DIR
            / f"{self._snapshot_filename(exp_id)}.json"
        )

    def _load_experiment_snapshot(self, project_dir: Path, exp_id: str) -> dict[str, Any] | None:
        payload = _load_json_file(self._experiment_snapshot_path(project_dir, exp_id))
        return payload if isinstance(payload, dict) else None

    def _save_experiment_snapshot(
        self, project_dir: Path, exp_id: str, payload: dict[str, Any]
    ) -> None:
        snapshot_path = self._experiment_snapshot_path(project_dir, exp_id)
        try:
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Failed to write experiment snapshot {}: {}", snapshot_path, exc)

    def _recover_snapshot_from_git_history(
        self, project_dir: Path, exp_id: str
    ) -> dict[str, Any] | None:
        if not (project_dir / ".git").is_dir():
            return None
        try:
            log_result = subprocess.run(
                ["git", "log", "--format=%H", "-n", "40", "--", PLAN_FILENAME],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=6,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if log_result.returncode != 0:
            return None

        commits = [line.strip() for line in log_result.stdout.splitlines() if line.strip()]
        for commit in commits:
            try:
                show_result = subprocess.run(
                    ["git", "show", f"{commit}:{PLAN_FILENAME}"],
                    cwd=project_dir,
                    capture_output=True,
                    text=True,
                    timeout=6,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if show_result.returncode != 0:
                continue
            try:
                plan = json.loads(show_result.stdout)
            except json.JSONDecodeError:
                continue
            if not isinstance(plan, dict):
                continue
            experiments = plan.get("experiments")
            if not isinstance(experiments, list):
                continue
            for item in experiments:
                if not isinstance(item, dict):
                    continue
                if item.get("id") != exp_id:
                    continue
                if _is_snapshot_candidate(item):
                    return _snapshot_from_experiment(item, source=f"git:{commit[:7]}")
        return None

    def _attach_experiment_snapshots(self, project_dir: Path, data: dict[str, Any]) -> None:
        experiments = data.get("experiments")
        if not isinstance(experiments, list):
            return

        for item in experiments:
            if not isinstance(item, dict):
                continue
            exp_id = item.get("id")
            if not isinstance(exp_id, str) or not exp_id.strip():
                continue

            snapshot = self._load_experiment_snapshot(project_dir, exp_id)
            if snapshot is None:
                if _is_snapshot_candidate(item):
                    snapshot = _snapshot_from_experiment(item, source="task_plan")
                    self._save_experiment_snapshot(project_dir, exp_id, snapshot)
                elif item.get("status") == "completed":
                    snapshot = self._recover_snapshot_from_git_history(project_dir, exp_id)
                    if snapshot is not None:
                        self._save_experiment_snapshot(project_dir, exp_id, snapshot)
            if snapshot is not None:
                item["snapshot"] = snapshot

    def _load_plan_data(self, session_id: str, *, reconcile: bool = True) -> dict[str, Any] | None:
        project_dir = self.projects_root / session_id
        plan_path = project_dir / PLAN_FILENAME
        if not plan_path.is_file():
            return None

        try:
            data = json.loads(plan_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"Failed to read {plan_path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"Unexpected non-object JSON in {plan_path}")

        if reconcile and self._reconcile_plan_data(project_dir, data):
            try:
                plan_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.warning("Failed to write reconciled {}: {}", plan_path, exc)

        self._attach_experiment_snapshots(project_dir, data)
        return data

    # ── CORS middleware ──────────────────────────────────────────────

    @web.middleware
    async def _cors_middleware(
        self,
        request: web.Request,
        handler: Any,
    ) -> web.StreamResponse:
        if request.method == "OPTIONS":
            resp = web.Response(status=204)
        else:
            resp = await handler(request)

        origin = request.headers.get("Origin", "*")
        allowed = self.config.cors_origins
        if "*" in allowed:
            resp.headers["Access-Control-Allow-Origin"] = origin
        elif origin in allowed:
            resp.headers["Access-Control-Allow-Origin"] = origin

        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp

    # ── WebSocket handler ────────────────────────────────────────────

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        session_id: str | None = None

        async for raw in ws:
            if raw.type != web.WSMsgType.TEXT:
                continue

            try:
                data: dict = json.loads(raw.data)
            except (json.JSONDecodeError, TypeError):
                await ws.send_json({"type": "error", "content": "Invalid JSON"})
                continue

            msg_type = data.get("type")

            if msg_type == "message":
                session_id = data.get("session_id", session_id)
                user_id = data.get("user_id", session_id or "anonymous")
                content = data.get("content", "")
                media = data.get("media", [])
                run_mode = _normalize_run_mode(data.get("mode"))
                agent_profile = _normalize_agent_profile(data.get("agent_profile"))
                incoming_policy = _normalize_automation_policy(data.get("automation_policy"))

                if session_id is None:
                    await ws.send_json(
                        {"type": "error", "content": "session_id required"}
                    )
                    continue

                self._clients[session_id] = ws
                project_dir = str(self.projects_root / session_id)
                project_dir_path = Path(project_dir)
                meta = self._persist_project_runtime_preferences(
                    project_dir_path,
                    run_mode=run_mode,
                    agent_profile=agent_profile,
                    automation_policy=incoming_policy,
                )
                effective_policy = _normalize_automation_policy(meta.get("automation_policy"))
                plan_ids_before = _extract_plan_experiment_ids(project_dir_path)
                guard = guard_task_plan_file(project_dir_path, auto_fix=True)
                guard_notice: str | None = None
                if guard.get("fixed"):
                    plan_ids_after = _extract_plan_experiment_ids(project_dir_path)
                    reassignments = _detect_guard_id_reassignments(plan_ids_before, plan_ids_after)
                    guard_notice = _build_task_plan_guard_notice(reassignments)
                if guard.get("fixed"):
                    self._audit(
                        source="system",
                        action="task_plan_guard_auto_fix_applied",
                        session_id=session_id,
                        project_dir=project_dir_path,
                        details={"issues_after_fix": guard.get("issues", [])[:5]},
                    )
                    if guard_notice:
                        self._audit(
                            source="system",
                            action="task_plan_guard_id_reassigned",
                            session_id=session_id,
                            project_dir=project_dir_path,
                            details={"notice": guard_notice},
                        )
                elif guard.get("blocking"):
                    self._audit(
                        source="system",
                        action="task_plan_guard_blocking_issue",
                        session_id=session_id,
                        project_dir=project_dir_path,
                        details={"issues": guard.get("issues", [])[:5]},
                    )
                self._audit(
                    source="ui",
                    action="ws_message_received",
                    session_id=session_id,
                    project_dir=project_dir_path,
                    details={
                        "user_id": user_id,
                        "run_mode": run_mode,
                        "agent_profile": agent_profile,
                        "has_automation_policy": bool(effective_policy),
                        "goal_count": len(effective_policy.get("goals", [])) if effective_policy else 0,
                        "content_preview": self._preview(content),
                        "media_count": len(media) if isinstance(media, list) else 0,
                    },
                )
                SessionManager(Path(project_dir)).append_ui_event(
                    key=f"web:{session_id}",
                    role="user",
                    content=content,
                    msg_type="response",
                    metadata={"_user": True},
                )
                metadata: dict[str, Any] = {
                    "source": "web",
                    "project_dir": project_dir,
                    "run_mode": run_mode,
                    "agent_profile": agent_profile,
                }
                if effective_policy:
                    metadata["automation_policy"] = effective_policy
                if self._ui_instructions:
                    metadata["_ui_system_instructions"] = self._ui_instructions
                if guard_notice:
                    metadata["_task_plan_guard_notice"] = guard_notice
                await self._handle_message(
                    sender_id=user_id,
                    chat_id=session_id,
                    content=content,
                    media=media,
                    metadata=metadata,
                    session_key=f"web:{session_id}",
                )
            elif msg_type == "set_mode":
                session_id = data.get("session_id", session_id)
                user_id = data.get("user_id", session_id or "anonymous")
                run_mode = _normalize_run_mode(data.get("mode"))

                if session_id is None:
                    await ws.send_json(
                        {"type": "error", "content": "session_id required"}
                    )
                    continue

                self._clients[session_id] = ws
                project_dir = str(self.projects_root / session_id)
                self._audit(
                    source="ui",
                    action="ws_set_mode_received",
                    session_id=session_id,
                    project_dir=Path(project_dir),
                    details={
                        "user_id": user_id,
                        "run_mode": run_mode,
                    },
                )
                metadata = {
                    "source": "web",
                    "project_dir": project_dir,
                    "run_mode": run_mode,
                    "_control": "set_mode",
                }
                await self._handle_message(
                    sender_id=user_id,
                    chat_id=session_id,
                    content="__set_mode__",
                    media=[],
                    metadata=metadata,
                    session_key=f"web:{session_id}",
                )
            elif msg_type == "bind":
                session_id = data.get("session_id", session_id)
                user_id = data.get("user_id", session_id or "anonymous")
                if session_id is None:
                    await ws.send_json(
                        {"type": "error", "content": "session_id required"}
                    )
                    continue
                self._clients[session_id] = ws
                project_dir = str(self.projects_root / session_id)
                self._audit(
                    source="ui",
                    action="ws_bind_received",
                    session_id=session_id,
                    project_dir=Path(project_dir),
                    details={"user_id": user_id},
                )

        # Client disconnected
        if session_id and self._clients.get(session_id) is ws:
            del self._clients[session_id]
            logger.info("WebSocket client disconnected: {}", session_id)

        return ws

    # ── REST endpoints ───────────────────────────────────────────────

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "service": "medpilot-gateway",
            "channel": self.name,
            "running": self._running,
            "connected_clients": len(self._clients),
        })

    async def _handle_version(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "service": "medpilot-gateway",
            "agent_version": __version__,
            "api_contract": _API_CONTRACT_VERSION,
            "uptime_seconds": int(max(time.monotonic() - self._boot_ts, 0)),
        })

    async def _handle_status(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "channel": self.name,
            "running": self._running,
            "connected_clients": len(self._clients),
            "uptime_host": f"{self.config.host}:{self.config.port}",
            "projects_root": str(self.projects_root),
        })

    async def _handle_sessions(self, _request: web.Request) -> web.Response:
        sessions = [
            {"session_id": sid, "connected": not ws.closed}
            for sid, ws in self._clients.items()
        ]
        return web.json_response({"sessions": sessions})

    @staticmethod
    def _history_entry_key(entry: dict[str, Any]) -> tuple[str, str, bool, str]:
        metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
        return (
            str(entry.get("timestamp", "")),
            str(entry.get("type", "")),
            bool(metadata.get("_user", False)),
            str(entry.get("content", "")),
        )

    @staticmethod
    def _history_entry_soft_key(entry: dict[str, Any]) -> tuple[str, str, bool]:
        metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
        return (
            str(entry.get("timestamp", "")),
            str(entry.get("type", "")),
            bool(metadata.get("_user", False)),
        )

    def _load_audit_history_entries(self, session_id: str) -> list[dict[str, Any]]:
        """Best-effort fallback for older sessions missing persisted chat messages."""
        project_dir = self.projects_root / session_id
        audit_file = project_dir / _PROJECT_AUDIT_REL_PATH
        if not audit_file.is_file():
            return []
        rows: list[dict[str, Any]] = []
        try:
            for idx, line in enumerate(audit_file.read_text(encoding="utf-8").splitlines()):
                if not line.strip():
                    continue
                item = json.loads(line)
                action = item.get("action")
                details = item.get("details") if isinstance(item.get("details"), dict) else {}
                if action == "ws_message_received":
                    content = details.get("content_preview")
                    if not isinstance(content, str) or not content:
                        continue
                    rows.append({
                        "id": f"audit-{session_id}-u-{idx}",
                        "timestamp": item.get("timestamp") or "",
                        "content": content,
                        "type": "response",
                        "metadata": {"_user": True},
                    })
                elif action in {"ws_outbound_sent", "ws_outbound_dropped"}:
                    content = details.get("content_preview")
                    if not isinstance(content, str) or not content:
                        continue
                    raw_type = details.get("type")
                    entry_type = raw_type if raw_type in {"response", "progress", "tool_call", "error"} else "response"
                    rows.append({
                        "id": f"audit-{session_id}-a-{idx}",
                        "timestamp": item.get("timestamp") or "",
                        "content": content,
                        "type": entry_type,
                        "metadata": {},
                    })
        except (json.JSONDecodeError, OSError):
            return []
        return rows

    def _load_history_entries(self, session_id: str) -> list[dict[str, Any]]:
        project_dir = self.projects_root / session_id
        if not project_dir.is_dir():
            return []

        manager = SessionManager(project_dir)
        session_key = f"web:{session_id}"
        session = manager.get_or_create(session_key)
        ui_entries = manager.get_ui_history(session_key)
        merged: list[dict[str, Any]] = list(ui_entries)
        seen_exact = {self._history_entry_key(entry) for entry in merged}
        seen_soft = {self._history_entry_soft_key(entry) for entry in merged}

        # Always keep tool-call trace from session messages; it's not persisted in UI events.
        for idx, msg in enumerate(session.messages):
            if msg.get("role") != "assistant":
                continue
            timestamp = msg.get("timestamp") or ""
            for tool_idx, tool_call in enumerate(msg.get("tool_calls") or []):
                if not isinstance(tool_call, dict):
                    continue
                entry = {
                    "id": f"history-{session_id}-{idx}-tool-{tool_idx}",
                    "timestamp": timestamp,
                    "content": _format_tool_call(tool_call),
                    "type": "tool_call",
                    "metadata": {},
                }
                exact_key = self._history_entry_key(entry)
                if exact_key in seen_exact:
                    continue
                seen_exact.add(exact_key)
                seen_soft.add(self._history_entry_soft_key(entry))
                merged.append(entry)

        # Legacy fallback: only synthesize user/assistant from session messages when
        # no UI-level history exists.
        if not ui_entries:
            for idx, msg in enumerate(session.messages):
                timestamp = msg.get("timestamp") or ""
                role = msg.get("role")
                if role == "user":
                    content = _stringify_history_content(msg.get("content"))
                    if not content:
                        continue
                    entry = {
                        "id": f"history-{session_id}-{idx}-user",
                        "timestamp": timestamp,
                        "content": content,
                        "type": "response",
                        "metadata": {"_user": True},
                    }
                elif role == "assistant":
                    content = _stringify_history_content(msg.get("content"))
                    if not content:
                        continue
                    entry = {
                        "id": f"history-{session_id}-{idx}-assistant",
                        "timestamp": timestamp,
                        "content": content,
                        "type": "response",
                        "metadata": {},
                    }
                else:
                    continue
                exact_key = self._history_entry_key(entry)
                soft_key = self._history_entry_soft_key(entry)
                if exact_key in seen_exact or soft_key in seen_soft:
                    continue
                seen_exact.add(exact_key)
                seen_soft.add(soft_key)
                merged.append(entry)

        # Audit preview fallback is strictly for very old/sparse sessions.
        if not merged:
            for entry in self._load_audit_history_entries(session_id):
                exact_key = self._history_entry_key(entry)
                soft_key = self._history_entry_soft_key(entry)
                if exact_key in seen_exact or soft_key in seen_soft:
                    continue
                seen_exact.add(exact_key)
                seen_soft.add(soft_key)
                merged.append(entry)

        merged.sort(key=lambda item: (str(item.get("timestamp", "")), str(item.get("id", ""))))
        return merged

    async def _handle_history(self, request: web.Request) -> web.Response:
        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        return web.json_response({
            "session_id": session_id,
            "entries": self._load_history_entries(session_id),
        })

    async def _handle_config(self, request: web.Request) -> web.Response:
        """Allow the UI to configure the projects root path."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid JSON"}, status=400)

        config_path = config_loader.get_config_path().expanduser().resolve()
        persisted = False
        if "projects_root" in body:
            raw_root = body["projects_root"]
            if not isinstance(raw_root, str):
                return web.json_response(
                    {"error": "projects_root must be a string"},
                    status=400,
                )

            new_root = Path(raw_root).expanduser().resolve()
            current_root = self.projects_root.expanduser().resolve()
            if new_root != current_root:
                self.projects_root = new_root
                self._audit(
                    source="ui",
                    action="api_projects_root_updated",
                    details={"projects_root": str(new_root)},
                )
                logger.info("Projects root updated to {}", new_root)

            try:
                config_path = self._persist_projects_root_to_config(self.projects_root)
                persisted = True
            except OSError as exc:
                logger.warning(
                    "Failed to persist projects root {} to config {}: {}",
                    self.projects_root,
                    config_path,
                    exc,
                )
                return web.json_response(
                    {
                        "error": f"failed to persist workspace config: {exc}",
                        "projects_root": str(self.projects_root),
                        "config_path": str(config_path),
                    },
                    status=500,
                )

        return web.json_response({
            "projects_root": str(self.projects_root),
            "config_path": str(config_path),
            "persisted": persisted,
        })

    def _persist_projects_root_to_config(self, projects_root: Path) -> Path:
        """Persist workspace root to the active runtime config file."""
        config_path = config_loader.get_config_path().expanduser().resolve()
        runtime_config = config_loader.load_config(config_path)
        runtime_config.agents.defaults.workspace = str(projects_root)
        config_loader.save_config(runtime_config, config_path)
        return config_path

    def _workspace_root_for_access(self) -> Path:
        """Return the root path used for workspace access checks."""
        return self.projects_root.expanduser().resolve()

    def _resolve_probe_path(self, raw_path: str) -> tuple[Path | None, str | None]:
        """Resolve a UI-provided data path using agent-like workspace rules."""
        path_text = raw_path.strip()
        if not path_text:
            return None, "path required"

        root = self._workspace_root_for_access()
        candidate = Path(path_text).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as exc:
            return None, f"invalid path: {exc}"

        if self.restrict_to_workspace:
            try:
                resolved.relative_to(root)
            except ValueError:
                return None, f"path is outside workspace: {root}"
        return resolved, None

    async def _handle_validate_data_path(self, request: web.Request) -> web.Response:
        """Validate whether a server-side data path is visible to the agent."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid JSON"}, status=400)

        raw_path = body.get("path") if isinstance(body, dict) else None
        if not isinstance(raw_path, str):
            return web.json_response({"ok": False, "error": "path must be a string"})

        resolved, err = self._resolve_probe_path(raw_path)
        if err or resolved is None:
            return web.json_response({"ok": False, "error": err or "invalid path"})

        if not resolved.exists():
            return web.json_response({
                "ok": False,
                "error": "path not found",
                "resolved_path": str(resolved),
            })

        if resolved.is_file():
            try:
                with resolved.open("rb"):
                    pass
            except OSError as exc:
                return web.json_response({
                    "ok": False,
                    "error": f"file is not readable: {exc}",
                    "resolved_path": str(resolved),
                })
            return web.json_response({
                "ok": True,
                "kind": "file",
                "resolved_path": str(resolved),
            })

        if resolved.is_dir():
            try:
                next(resolved.iterdir(), None)
            except OSError as exc:
                return web.json_response({
                    "ok": False,
                    "error": f"directory is not readable: {exc}",
                    "resolved_path": str(resolved),
                })
            return web.json_response({
                "ok": True,
                "kind": "directory",
                "resolved_path": str(resolved),
            })

        return web.json_response({
            "ok": False,
            "error": "path is neither a regular file nor directory",
            "resolved_path": str(resolved),
        })

    async def _handle_plan(self, request: web.Request) -> web.Response:
        """Serve task_plan.json, scoped to a project when session_id is given."""
        session_id = request.query.get("session_id")
        if not session_id:
            return web.json_response(None)

        try:
            data = self._load_plan_data(session_id)
        except ValueError as exc:
            logger.warning(str(exc))
            return web.json_response({"error": str(exc)}, status=500)
        if data is None:
            return web.json_response(None)
        return web.json_response(data)

    async def _handle_plan_contract(self, request: web.Request) -> web.Response:
        """Serve resolved task-plan contract requirements for one project."""
        session_id = (request.query.get("session_id") or "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        project_dir = self.projects_root / session_id
        if not project_dir.is_dir():
            return web.json_response({"error": "project not found"}, status=404)

        meta = self._ensure_project_meta(project_dir)
        profile = _normalize_agent_profile(meta.get("agent_profile"))
        contract_version = _normalize_contract_version(meta.get("contract_version"))
        contract = get_task_plan_contract(
            profile=profile, contract_version=contract_version
        )
        return web.json_response(contract)

    async def _handle_plan_lint(self, request: web.Request) -> web.Response:
        """Validate and optionally auto-fix a project's task plan."""
        session_id = (request.query.get("session_id") or "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)

        auto_fix = (request.query.get("auto_fix", "1") or "1").strip().lower() not in {"0", "false", "no"}
        project_dir = self.projects_root / session_id
        if not project_dir.is_dir():
            return web.json_response({"error": "project not found"}, status=404)

        result = guard_task_plan_file(project_dir, auto_fix=auto_fix)
        self._audit(
            source="ui",
            action="api_plan_lint",
            session_id=session_id,
            project_dir=project_dir,
            details={
                "auto_fix": auto_fix,
                "ok": result.get("ok"),
                "fixed": result.get("fixed"),
                "blocking": result.get("blocking"),
                "issue_count": len(result.get("issues", [])),
            },
        )
        return web.json_response(result)

    def _project_meta_path(self, project_dir: Path) -> Path:
        return project_dir / PROJECT_META_DIRNAME / PROJECT_META_FILENAME

    def _is_project_dir(self, project_dir: Path) -> bool:
        return project_dir.is_dir() and project_dir.name.startswith(PROJECT_DIR_PREFIX)

    def _load_project_meta(self, project_dir: Path) -> dict[str, Any]:
        meta_path = self._project_meta_path(project_dir)
        if not meta_path.is_file():
            return {}
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def _default_project_meta(self, project_id: str) -> dict[str, Any]:
        now = f"{datetime.utcnow().isoformat()}Z"
        return {
            "id": project_id,
            "display_name": project_id,
            "run_mode": PROJECT_META_DEFAULT_RUN_MODE,
            "agent_profile": PROJECT_META_DEFAULT_AGENT_PROFILE,
            "contract_version": PROJECT_META_DEFAULT_CONTRACT_VERSION,
            "automation_policy": None,
            "created_at": now,
            "updated_at": now,
            "schema_version": PROJECT_META_SCHEMA_VERSION,
        }

    def _write_project_meta(self, project_dir: Path, meta: dict[str, Any]) -> None:
        meta_path = self._project_meta_path(project_dir)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def _ensure_project_meta(self, project_dir: Path) -> dict[str, Any]:
        project_id = project_dir.name
        current = self._load_project_meta(project_dir)
        baseline = self._default_project_meta(project_id)
        meta = {**baseline, **current}

        display_name = meta.get("display_name")
        if not isinstance(display_name, str) or not display_name.strip():
            meta["display_name"] = project_id
        else:
            meta["display_name"] = display_name.strip()

        if meta.get("id") != project_id:
            meta["id"] = project_id

        meta["run_mode"] = _normalize_run_mode(meta.get("run_mode"))
        meta["agent_profile"] = _normalize_agent_profile(meta.get("agent_profile"))
        meta["contract_version"] = _normalize_contract_version(meta.get("contract_version"))
        meta["automation_policy"] = _normalize_automation_policy(meta.get("automation_policy"))

        if not isinstance(meta.get("schema_version"), int):
            meta["schema_version"] = PROJECT_META_SCHEMA_VERSION

        if not isinstance(meta.get("created_at"), str) or not meta["created_at"]:
            meta["created_at"] = baseline["created_at"]
        if not isinstance(meta.get("updated_at"), str) or not meta["updated_at"]:
            meta["updated_at"] = baseline["updated_at"]

        self._write_project_meta(project_dir, meta)
        return meta

    def _persist_project_runtime_preferences(
        self,
        project_dir: Path,
        *,
        run_mode: str,
        agent_profile: str,
        automation_policy: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Persist runtime preferences for websocket-driven project sessions."""
        project_dir.mkdir(parents=True, exist_ok=True)
        meta = self._ensure_project_meta(project_dir)
        changed = False

        if meta.get("run_mode") != run_mode:
            meta["run_mode"] = run_mode
            changed = True
        if meta.get("agent_profile") != agent_profile:
            meta["agent_profile"] = agent_profile
            changed = True

        normalized_policy = _normalize_automation_policy(automation_policy)
        if meta.get("automation_policy") != normalized_policy:
            meta["automation_policy"] = normalized_policy
            changed = True

        if changed:
            meta["updated_at"] = f"{datetime.utcnow().isoformat()}Z"
            self._write_project_meta(project_dir, meta)
        return meta

    async def _handle_list_projects(self, _request: web.Request) -> web.Response:
        """List PRJ-* project directories under projects_root with optional task_plan data."""
        if not self.projects_root.is_dir():
            return web.json_response({"projects": []})

        projects: list[dict[str, Any]] = []
        for d in sorted(self.projects_root.iterdir()):
            if not self._is_project_dir(d):
                continue

            meta = self._ensure_project_meta(d)
            info: dict[str, Any] = {
                "id": d.name,
                "display_name": str(meta.get("display_name", d.name)),
                "run_mode": str(meta.get("run_mode", PROJECT_META_DEFAULT_RUN_MODE)),
                "agent_profile": str(
                    meta.get("agent_profile", PROJECT_META_DEFAULT_AGENT_PROFILE)
                ),
                "contract_version": int(
                    _normalize_contract_version(meta.get("contract_version"))
                ),
                "automation_policy": _normalize_automation_policy(
                    meta.get("automation_policy")
                ),
                "has_meta": True,
            }
            plan_file = d / PLAN_FILENAME
            if plan_file.is_file():
                try:
                    plan = json.loads(plan_file.read_text(encoding="utf-8"))
                    info["title"] = plan.get("title", "")
                    info["status"] = plan.get("status", "in_progress")
                    info["core_question"] = plan.get("core_question", "")
                    info["started_at"] = plan.get("started_at", "")
                    info["has_plan"] = True
                except (json.JSONDecodeError, OSError):
                    info["has_plan"] = False
            else:
                info["has_plan"] = False
            projects.append(info)

        return web.json_response({"projects": projects})

    async def _handle_project_meta(self, request: web.Request) -> web.Response:
        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)

        project_dir = self.projects_root / session_id
        if not self._is_project_dir(project_dir):
            return web.json_response({"error": "project not found"}, status=404)

        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "JSON body must be an object"}, status=400)

        has_display_name = "display_name" in body
        has_run_mode = "run_mode" in body
        has_agent_profile = "agent_profile" in body
        has_contract_version = "contract_version" in body
        has_automation_policy = "automation_policy" in body
        if not any((
            has_display_name,
            has_run_mode,
            has_agent_profile,
            has_contract_version,
            has_automation_policy,
        )):
            return web.json_response(
                {
                    "error": (
                        "at least one of display_name/run_mode/agent_profile/"
                        "contract_version/automation_policy is required"
                    )
                },
                status=400,
            )

        display_name = body.get("display_name")
        if has_display_name and not isinstance(display_name, str):
            return web.json_response(
                {"error": "display_name must be a string"}, status=400
            )

        run_mode = body.get("run_mode")
        if has_run_mode and not isinstance(run_mode, str):
            return web.json_response({"error": "run_mode must be a string"}, status=400)

        agent_profile = body.get("agent_profile")
        if has_agent_profile and not isinstance(agent_profile, str):
            return web.json_response(
                {"error": "agent_profile must be a string"}, status=400
            )

        contract_version = body.get("contract_version")
        if has_contract_version and not isinstance(contract_version, int):
            return web.json_response(
                {"error": "contract_version must be an integer"}, status=400
            )

        automation_policy = body.get("automation_policy")
        if has_automation_policy and not isinstance(automation_policy, (dict, type(None))):
            return web.json_response(
                {"error": "automation_policy must be an object or null"}, status=400
            )

        meta = self._ensure_project_meta(project_dir)
        if has_display_name:
            meta["display_name"] = (display_name or "").strip() or session_id
        if has_run_mode:
            meta["run_mode"] = _normalize_run_mode(run_mode)
        if has_agent_profile:
            meta["agent_profile"] = _normalize_agent_profile(agent_profile)
        if has_contract_version:
            meta["contract_version"] = _normalize_contract_version(contract_version)
        if has_automation_policy:
            meta["automation_policy"] = _normalize_automation_policy(automation_policy)
        meta["updated_at"] = f"{datetime.utcnow().isoformat()}Z"
        self._write_project_meta(project_dir, meta)

        self._audit(
            source="ui",
            action="api_project_meta_updated",
            session_id=session_id,
            project_dir=project_dir,
            details={
                "display_name": meta.get("display_name"),
                "run_mode": meta.get("run_mode"),
                "agent_profile": meta.get("agent_profile"),
                "contract_version": meta.get("contract_version"),
                "automation_policy": meta.get("automation_policy"),
            },
        )
        return web.json_response(
            {
                "id": session_id,
                "display_name": meta.get("display_name"),
                "run_mode": meta.get("run_mode"),
                "agent_profile": meta.get("agent_profile"),
                "contract_version": meta.get("contract_version"),
                "automation_policy": meta.get("automation_policy"),
                "meta": meta,
            }
        )

    async def _handle_delete_project(self, request: web.Request) -> web.Response:
        """Delete a project directory from disk."""
        session_id = request.query.get("session_id")
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)

        project_dir = self.projects_root / session_id
        if not project_dir.is_dir():
            self._audit(
                source="ui",
                action="api_delete_project_missing",
                session_id=session_id,
                details={"reason": "not found"},
            )
            return web.json_response({"deleted": False, "reason": "not found"})

        try:
            self._audit(
                source="ui",
                action="api_delete_project_requested",
                session_id=session_id,
                project_dir=project_dir,
            )
            shutil.rmtree(project_dir)
            self._audit(
                source="ui",
                action="api_delete_project_completed",
                session_id=session_id,
            )
            logger.info("Deleted project directory: {}", project_dir)
            return web.json_response({"deleted": True})
        except OSError as exc:
            self._audit(
                source="ui",
                action="api_delete_project_failed",
                session_id=session_id,
                details={"error": self._preview(str(exc), limit=400)},
            )
            logger.warning("Failed to delete {}: {}", project_dir, exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_upload_project_files(self, request: web.Request) -> web.Response:
        """Upload files into projects_root/<session_id>/data for web clients."""
        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        target = (request.query.get("target") or "data").strip().lower()
        if target not in {"data", "references"}:
            return web.json_response(
                {"error": "target must be one of: data, references"}, status=400
            )

        try:
            multipart = await request.multipart()
        except Exception:
            return web.json_response({"error": "expected multipart/form-data"}, status=400)

        project_dir = self.projects_root / session_id
        upload_dir = project_dir / target
        try:
            upload_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Failed to create upload directory {}: {}", upload_dir, exc)
            return web.json_response({"error": str(exc)}, status=500)

        uploaded: list[dict[str, Any]] = []
        extracted: list[dict[str, Any]] = []

        while True:
            part = await multipart.next()
            if part is None:
                break
            if part.name != "files":
                await part.release()
                continue
            if not part.filename:
                await part.release()
                continue

            safe_name = _safe_upload_name(part.filename)
            if not safe_name:
                await part.release()
                continue
            if target == "references":
                suffix = Path(safe_name).suffix.lower()
                if suffix not in {".pdf", ".zip"}:
                    return web.json_response(
                        {
                            "error": (
                                "references uploads only support .pdf and .zip files"
                            )
                        },
                        status=400,
                    )

            destination = _next_available_path(upload_dir, safe_name)
            size = 0
            try:
                with destination.open("wb") as f:
                    while True:
                        chunk = await part.read_chunk()
                        if not chunk:
                            break
                        f.write(chunk)
                        size += len(chunk)
            except OSError as exc:
                logger.warning("Failed to write uploaded file {}: {}", destination, exc)
                return web.json_response({"error": str(exc)}, status=500)

            uploaded.append({
                "name": destination.name,
                "path": destination.relative_to(project_dir).as_posix(),
                "size": size,
            })
            if target == "references" and destination.suffix.lower() == ".zip":
                try:
                    extracted.extend(
                        _extract_zip_into_references(destination, upload_dir, project_dir)
                    )
                except ValueError as exc:
                    return web.json_response({"error": str(exc)}, status=400)
                except OSError as exc:
                    logger.warning(
                        "Failed to extract reference archive {}: {}",
                        destination,
                        exc,
                    )
                    return web.json_response({"error": str(exc)}, status=500)

        if not uploaded:
            return web.json_response({"error": "no files uploaded"}, status=400)

        self._audit(
            source="ui",
            action="api_project_files_uploaded",
            session_id=session_id,
            project_dir=project_dir,
            details={
                "target": target,
                "count": len(uploaded),
                "files": [item.get("path", "") for item in uploaded],
                "extracted": [item.get("path", "") for item in extracted],
            },
        )

        return web.json_response({
            "session_id": session_id,
            "target": target,
            "uploaded": uploaded,
            "extracted": extracted,
        })

    async def _handle_project_artifact(self, request: web.Request) -> web.Response:
        """Serve a project file under projects_root/<session_id> by relative path."""
        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)

        rel_path = request.query.get("path", "").strip()
        if not rel_path:
            return web.json_response({"error": "path required"}, status=400)

        project_dir = (self.projects_root / session_id).resolve()
        if not project_dir.is_dir():
            return web.json_response({"error": "project not found"}, status=404)

        candidate = (project_dir / rel_path).resolve()
        try:
            candidate.relative_to(project_dir)
        except ValueError:
            return web.json_response({"error": "invalid artifact path"}, status=400)

        if not candidate.is_file():
            return web.json_response({"error": "artifact not found"}, status=404)

        return web.FileResponse(candidate)

    def _skill_plugin_manager(self, session_id: str) -> SkillPluginManager:
        project_dir = self.projects_root / session_id
        project_dir.mkdir(parents=True, exist_ok=True)
        return SkillPluginManager(project_dir)

    async def _handle_skill_plugins_list(self, request: web.Request) -> web.Response:
        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        manager = self._skill_plugin_manager(session_id)
        return web.json_response({"plugins": manager.list_plugins()})

    async def _handle_skill_plugins_install(self, request: web.Request) -> web.Response:
        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        manager = self._skill_plugin_manager(session_id)

        content_type = request.headers.get("Content-Type", "").lower()
        try:
            if content_type.startswith("multipart/form-data"):
                multipart = await request.multipart()
                zip_path: Path | None = None
                zip_name: str | None = None
                while True:
                    part = await multipart.next()
                    if part is None:
                        break
                    if part.name != "zip":
                        await part.release()
                        continue
                    if not part.filename:
                        await part.release()
                        continue
                    zip_name = part.filename
                    with tempfile.NamedTemporaryFile(
                        prefix="skill-plugin-",
                        suffix=".zip",
                        delete=False,
                    ) as tmp:
                        while True:
                            chunk = await part.read_chunk()
                            if not chunk:
                                break
                            tmp.write(chunk)
                        zip_path = Path(tmp.name)
                if zip_path is None:
                    return web.json_response({"error": "zip file field 'zip' is required"}, status=400)
                try:
                    installed = manager.install_from_zip(zip_path, archive_name_hint=zip_name)
                finally:
                    try:
                        zip_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            else:
                try:
                    body = await request.json()
                except (json.JSONDecodeError, TypeError):
                    return web.json_response({"error": "invalid JSON"}, status=400)
                source_path = body.get("path") if isinstance(body, dict) else None
                if not isinstance(source_path, str) or not source_path.strip():
                    return web.json_response({"error": "directory path is required"}, status=400)
                installed = manager.install_from_directory(Path(source_path))
        except SkillPluginError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        return web.json_response({
            "installed": installed,
            "plugins": manager.list_plugins(),
        })

    async def _handle_skill_plugins_state(self, request: web.Request) -> web.Response:
        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid JSON"}, status=400)

        scope = body.get("scope")
        target_type = body.get("target_type")
        plugin_id = body.get("plugin_id")
        enabled = body.get("enabled")
        target_id = body.get("target_id")
        if not isinstance(enabled, bool):
            return web.json_response({"error": "enabled must be a boolean"}, status=400)

        manager = self._skill_plugin_manager(session_id)
        try:
            manager.set_enabled(
                scope=scope,
                plugin_id=plugin_id,
                target_type=target_type,
                enabled=enabled,
                target_id=target_id,
            )
        except SkillPluginError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        return web.json_response({"plugins": manager.list_plugins()})

    async def _handle_skill_plugins_uninstall(self, request: web.Request) -> web.Response:
        session_id = request.match_info.get("session_id", "").strip()
        plugin_id = request.match_info.get("plugin_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        if not plugin_id:
            return web.json_response({"error": "plugin_id required"}, status=400)

        manager = self._skill_plugin_manager(session_id)
        try:
            manager.uninstall(plugin_id)
        except SkillPluginError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        return web.json_response({"uninstalled": plugin_id, "plugins": manager.list_plugins()})
