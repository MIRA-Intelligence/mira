"""UI channel – exposes a WebSocket + HTTP API for browser/Electron clients.

Historical note: this module was previously named ``web``. It has been renamed
to ``ui`` to better reflect its purpose (the channel that fronts Mira's
desktop/browser UI). The underlying transport is still WebSocket + HTTP.
The ``web`` channel name remains accepted on inbound config and on disk for
backward compatibility – see ``mira_engine/config/loader.py`` and the
session-key fallback in :class:`UiChannel`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote
from uuid import uuid4

from aiohttp import ClientError, ClientSession, ClientTimeout, WSCloseCode, web
from loguru import logger

from mira_engine import __version__
from mira_engine.agent.skill_plugins import SkillPluginError, SkillPluginManager
from mira_engine.agent.tools.sandbox import sandbox_status
from mira_engine.bus.events import InboundMessage, OutboundMessage
from mira_engine.bus.queue import MessageBus
from mira_engine.channels.base import BaseChannel
from mira_engine.cli.agent_service import _current_engine_identity
from mira_engine.community import approvals as community_approvals
from mira_engine.community import inbox as community_inbox
from mira_engine.community.public_profile import (
    PublicProfileError,
    normalize_profile_patch,
    normalize_public_profile,
    pairing_payload,
    suggested_agent_name,
)
from mira_engine.config import loader as config_loader
from mira_engine.config import team_layer
from mira_engine.config.paths import get_runtime_subdir, get_workspace_path
from mira_engine.config.schema import Config, KnowledgeConfig, UiChannelConfig, is_custom_provider_id
from mira_engine.knowledge import PROJECT_META_KEY, clean_kb_ids
from mira_engine.knowledge.api import KnowledgeHttpApi
from mira_engine.knowledge.service import KnowledgeService
from mira_engine.utils.slide_decks import deck_response, is_deck_file
from mira_engine.config.ui_runtime import (
    apply_ui_runtime_update,
    build_ui_runtime_payload,
    save_ui_runtime_update,
)
from mira_engine.modules import module_system_instructions
from mira_engine.organization import ORGANIZATION_FILENAME, OrganizationStore
from mira_engine.projects import (
    PROJECT_DIR_INDEX_FILENAME,
    PROJECT_META_DEFAULT_AGENT_PROFILE,
    PROJECT_META_DEFAULT_CONTRACT_VERSION,
    PROJECT_META_DEFAULT_RUN_MODE,
    PROJECT_META_STRICT_CONTRACT_VERSION,
    PROJECT_WORKSPACE_FILENAME,
    ProjectDirectoryExistsError,
    ProjectRef,
    ProjectRegistry,
    slugify_project_id,
    validate_project_id,
)
from mira_engine.providers.model_fetch import (
    ModelCache,
    ModelFetchError,
    fetch_models_to_cache,
    fetch_provider_models,
    is_cache_stale,
    read_model_cache,
)
from mira_engine.providers.registry import find_by_name
from mira_engine.security.gateway_auth import (
    EXEMPT_PATHS as GATEWAY_EXEMPT_PATHS,
    allowed_origins,
    origin_allowed,
    presented_token,
    resolve_gateway_token,
    token_matches,
    warn_if_exposed,
)
from mira_engine.security.host_policy import (
    LOCKED_ERROR_CODE,
    HostPolicyError,
    PolicyLockedError,
    load_host_policy,
)
from mira_engine.session.manager import SessionManager
from mira_engine.task_plan.guardrails import (
    get_task_plan_contract,
    guard_task_plan_file,
    reconcile_task_plan_data,
)
from mira_engine.workflows.http import WorkflowHttpApi
from mira_engine.workflows.input_paths import resolve_input_path

PLAN_FILENAME = "task_plan.json"
# Sentinel session id used by the UI to manage globally-scoped skill plugins
# without a selected project. Mirrors GLOBAL_SKILLS_SESSION_ID in the frontend.
_GLOBAL_SKILLS_SESSION_ID = "__global__"


def _resolve_ui_provider_name(config: Config, name: str) -> str | None:
    """Map a provider id from the UI to its config name (additional custom ids included)."""
    if not name:
        return None
    if name in config.providers.custom_providers:
        return name
    spec = find_by_name(name)
    return spec.name if spec else None


def host_policy_payload() -> dict[str, Any]:
    """The host policy summary for clients (locked settings, source)."""
    try:
        return load_host_policy().summary()
    except HostPolicyError as exc:
        return {"source": None, "groups": [], "locked": [], "plugin_install": False, "error": str(exc)}


def sandbox_status_payload() -> dict[str, Any]:
    try:
        exec_config = config_loader.load_config().tools.exec
    except Exception as exc:  # noqa: BLE001 - status must not fail
        return {"mode": None, "available": False, "reason": str(exc)}
    return sandbox_status(exec_config.sandbox, network=exec_config.sandbox_network)


def _knowledge_config() -> KnowledgeConfig:
    try:
        return config_loader.load_config().tools.knowledge
    except Exception:  # noqa: BLE001 - fall back to defaults on a broken config
        return KnowledgeConfig()


def _web_proxy() -> str | None:
    try:
        return config_loader.load_config().tools.web.proxy or None
    except Exception:  # noqa: BLE001
        return None


def plugin_install_refusal() -> web.Response | None:
    """403 when the host policy forbids installing or removing skill plugins."""
    try:
        policy = load_host_policy()
    except HostPolicyError as exc:
        return web.json_response({"error": str(exc), "code": LOCKED_ERROR_CODE}, status=403)
    if policy.plugin_install_disabled:
        return web.json_response(
            {"error": f"installing skill plugins is disabled by the host policy ({policy.source})",
             "code": LOCKED_ERROR_CODE},
            status=403,
        )
    return None
_ASSETS_DIR = Path(__file__).parent / "ui_assets"
_PROJECT_AUDIT_REL_PATH = Path(".mira") / "logs" / "actions.jsonl"
_GLOBAL_AUDIT_FILENAME = "project_actions.jsonl"
_PROJECT_EXPERIMENT_SNAPSHOT_REL_DIR = Path(".mira") / "snapshots" / "experiments"
_RECOVERED_CONCLUSION_PLACEHOLDER = "Recovered completed experiment artifacts from workspace."
_API_CONTRACT_VERSION = "v1"
_FEEDBACK_CONFIG_FILENAME = "mira-engine.feedback.json"
_FEEDBACK_TIMEOUT_SECONDS = 8
_FEEDBACK_AGENT_HANDLE = "@MIRAI"
_FEEDBACK_AGENT_DISPLAY_NAME = "MIRAI"


@dataclass(frozen=True)
class _FeedbackRelayConfig:
    feishu_webhook_url: str = ""
    feishu_secret: str = ""
    feishu_invite_url: str = ""
    feishu_mention_open_id: str = ""
    feishu_mention_name: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.feishu_webhook_url.strip())


def _string_value(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _first_present(*values: Any) -> str:
    for value in values:
        text = _string_value(value)
        if text:
            return text
    return ""


def _feedback_sidecar_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("MIRA_FEEDBACK_CONFIG_PATH")
    if configured:
        candidates.append(Path(configured).expanduser())
    try:
        candidates.append(Path(sys.executable).resolve().with_name(_FEEDBACK_CONFIG_FILENAME))
    except (OSError, ValueError):
        pass
    meipass = getattr(sys, "_MEIPASS", None)
    if isinstance(meipass, str) and meipass:
        candidates.append(Path(meipass) / _FEEDBACK_CONFIG_FILENAME)
    return candidates


def _load_feedback_sidecar() -> dict[str, Any]:
    for path in _feedback_sidecar_candidates():
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Ignoring invalid feedback relay config at {}", path)
            continue
        return raw if isinstance(raw, dict) else {}
    return {}


def _feedback_sidecar_sha256() -> str | None:
    for path in _feedback_sidecar_candidates():
        if not path.is_file():
            continue
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return None


def _nested_value(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _camel_key(key: str) -> str:
    parts = key.split("_")
    return parts[0] + "".join(part.capitalize() for part in parts[1:])


def _config_value(config: Any, key: str) -> Any:
    camel_key = _camel_key(key)
    if isinstance(config, dict):
        for candidate in (key, camel_key):
            if candidate in config:
                return config[candidate]
        return None
    return getattr(config, key, getattr(config, camel_key, None))


def _resolve_feedback_config(config: UiChannelConfig | Any) -> _FeedbackRelayConfig:
    configured = _config_value(config, "feedback")
    sidecar = _load_feedback_sidecar()
    feishu = sidecar.get("feishu") if isinstance(sidecar.get("feishu"), dict) else {}
    return _FeedbackRelayConfig(
        feishu_webhook_url=_first_present(
            os.environ.get("MIRA_FEISHU_WEBHOOK_URL"),
            _config_value(configured, "feishu_webhook_url"),
            _nested_value(feishu, "webhookUrl"),
            _nested_value(feishu, "webhook_url"),
        ),
        feishu_secret=_first_present(
            os.environ.get("MIRA_FEISHU_WEBHOOK_SECRET"),
            _config_value(configured, "feishu_secret"),
            _nested_value(feishu, "secret"),
        ),
        feishu_invite_url=_first_present(
            os.environ.get("MIRA_FEISHU_GROUP_INVITE_URL"),
            _config_value(configured, "feishu_invite_url"),
            _nested_value(feishu, "inviteUrl"),
            _nested_value(feishu, "invite_url"),
        ),
        feishu_mention_open_id=_first_present(
            os.environ.get("MIRA_FEISHU_MENTION_OPEN_ID"),
            _config_value(configured, "feishu_mention_open_id"),
            _nested_value(feishu, "mentionOpenId"),
            _nested_value(feishu, "mention_open_id"),
        ),
        feishu_mention_name=_first_present(
            os.environ.get("MIRA_FEISHU_MENTION_NAME"),
            _config_value(configured, "feishu_mention_name"),
            _nested_value(feishu, "mentionName"),
            _nested_value(feishu, "mention_name"),
        ),
    )


def _escape_feishu_text(input_text: str) -> str:
    return input_text.replace("<", "＜").replace(">", "＞")


def _feedback_text(payload: dict[str, Any], key: str, max_len: int, default: str = "") -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        return default
    return value.strip()[:max_len]


def _sanitize_feedback_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("feedback payload must be an object")
    title = _feedback_text(raw, "title", 120)
    body = _feedback_text(raw, "body", 4000)
    if not title or not body:
        raise ValueError("title and body are required")

    feedback_type = _feedback_text(raw, "type", 24, "other")
    if feedback_type not in {"bug", "feature", "question", "other"}:
        feedback_type = "other"
    severity = _feedback_text(raw, "severity", 24)
    if severity not in {"blocker", "critical", "normal", "minor"}:
        severity = ""

    contact_raw = raw.get("contact")
    contact = None
    if isinstance(contact_raw, dict):
        kind = _feedback_text(contact_raw, "kind", 24)
        value = _feedback_text(contact_raw, "value", 120)
        if kind in {"wechat", "phone", "email"} and value:
            contact = {"kind": kind, "value": value}

    return {
        "id": _feedback_text(raw, "id", 80, f"fb_{int(time.time())}"),
        "clientHandle": _feedback_text(raw, "clientHandle", 80, "anonymous"),
        "type": feedback_type,
        "severity": severity or None,
        "title": title,
        "body": body,
        "contact": contact,
        "appVersion": _feedback_text(raw, "appVersion", 80, __version__),
        "os": _feedback_text(raw, "os", 80, "unknown"),
        "route": _feedback_text(raw, "route", 200, "/"),
        "locale": _feedback_text(raw, "locale", 20, "unknown"),
        "createdAt": _feedback_text(raw, "createdAt", 80, datetime.utcnow().isoformat() + "Z"),
    }


def _build_feedback_agent_text(
    payload: dict[str, Any],
    mention_open_id: str = "",
    mention_name: str = "",
) -> str:
    type_label = {
        "bug": "Bug",
        "feature": "Feature",
        "question": "Question",
        "other": "Other",
    }
    severity_label = {
        "blocker": "阻塞",
        "critical": "严重",
        "normal": "一般",
        "minor": "轻微",
    }
    lines: list[str] = []
    if mention_open_id:
        display_name = mention_name or _FEEDBACK_AGENT_DISPLAY_NAME
        lines.append(
            f'<at user_id="{mention_open_id}">{_escape_feishu_text(display_name)}</at> '
            "请处理这条 MIRA feedback。"
        )
        lines.append("")
    else:
        lines.append(f"{_FEEDBACK_AGENT_HANDLE} 请处理这条 MIRA feedback。")
        lines.append("")

    lines.extend([
        f"【MIRA Feedback】{type_label[payload['type']]}",
        f"mira_feedback  tag={_escape_feishu_text(payload['type'])}  feedback_id={_escape_feishu_text(payload['id'])}",
        "",
        f"标题：{_escape_feishu_text(payload['title'])}",
        "",
        "内容：",
        _escape_feishu_text(payload["body"]),
        "",
        "元信息：",
        f"- 用户：{_escape_feishu_text(payload['clientHandle'])}",
        f"- 版本：{_escape_feishu_text(payload['appVersion'])}",
        f"- OS：{_escape_feishu_text(payload['os'])}",
        f"- 页面：{_escape_feishu_text(payload['route'])}",
        f"- 语言：{_escape_feishu_text(payload['locale'])}",
        f"- 时间：{_escape_feishu_text(payload['createdAt'])}",
    ])
    if payload.get("severity"):
        lines.insert(3, f"严重度：{severity_label[payload['severity']]}")
    contact = payload.get("contact")
    if isinstance(contact, dict):
        lines.append(
            f"- 联系：{_escape_feishu_text(contact['kind'])} · {_escape_feishu_text(contact['value'])}"
        )
    return "\n".join(lines)


def _feishu_sign(timestamp_sec: str, secret: str) -> str:
    key = f"{timestamp_sec}\n{secret}".encode("utf-8")
    digest = hmac.new(key, b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _resolve_project_dir_index_path() -> Path:
    """Locate the project-dirs index file, migrating from the legacy ``web`` dir.

    Until v0.4 the UI channel stored its project-dir index under
    ``~/.mira/web/project-dirs.json``. After the channel was renamed to ``ui``
    we prefer ``~/.mira/ui/project-dirs.json`` but transparently migrate any
    pre-existing legacy file so users keep their project list.
    """
    new_path = get_runtime_subdir("ui") / PROJECT_DIR_INDEX_FILENAME
    if new_path.exists():
        return new_path
    legacy_path = get_runtime_subdir("web") / PROJECT_DIR_INDEX_FILENAME
    if legacy_path.exists():
        try:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_path), str(new_path))
            logger.info(
                "Migrated UI channel project-dir index from {} to {}",
                legacy_path,
                new_path,
            )
        except Exception:
            logger.exception("Failed to migrate legacy project-dir index")
            return legacy_path
    return new_path


def _resolve_project_workspace_path() -> Path:
    return get_runtime_subdir("ui").parent / PROJECT_WORKSPACE_FILENAME


def _resolve_organization_path() -> Path:
    return _resolve_project_workspace_path().with_name(ORGANIZATION_FILENAME)


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


def _normalize_loop_mode(value: Any) -> str:
    """Normalize the UI's high-level app mode."""
    if isinstance(value, str):
        mode = value.strip().lower()
        if mode in {"normal", "project"}:
            return mode
    return "project"


def _normalize_agent_profile(value: Any) -> str:
    """Normalize UI agent profile with a conservative fallback."""
    if isinstance(value, str):
        profile = value.strip().lower()
        if profile in {"engineer", "research", "team"}:
            return profile
    return "research"


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


def _render_chat_context_attachment(
    source_session_id: str,
    entries: list[dict[str, Any]],
) -> tuple[str, int]:
    """Render user-visible chat turns as a project context attachment."""
    lines = [
        "# Conversation Context",
        "",
        f"Source chat: `{source_session_id}`",
        "",
        "This file preserves the conversation that preceded this project. "
        "Use the user messages and Mira responses as context for identifying the project's objective.",
    ]
    message_count = 0
    for entry in entries:
        if entry.get("type") != "response":
            continue
        content = _stringify_history_content(entry.get("content")).strip()
        if not content:
            continue
        metadata = entry.get("metadata")
        is_user = isinstance(metadata, dict) and metadata.get("_user") is True
        role = "User" if is_user else "Mira"
        timestamp = _string_value(entry.get("timestamp"))
        heading = f"## {role}"
        if timestamp:
            heading += f" — {timestamp}"
        lines.extend(["", heading, "", content])
        message_count += 1
    return "\n".join(lines).rstrip() + "\n", message_count


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


def _safe_upload_relative_parts(filename: str) -> list[str] | None:
    """Normalize a browser-provided upload path without allowing traversal."""
    normalized = filename.replace("\\", "/").replace("\x00", "")
    if len(normalized) >= 3 and normalized[1] == ":" and normalized[2] == "/":
        return None
    raw = Path(normalized)
    if raw.is_absolute():
        return None

    safe_parts: list[str] = []
    for part in raw.parts:
        clean = part.strip()
        if clean in {"", "."}:
            continue
        if clean == "..":
            return None
        safe_parts.append(clean)
    if not safe_parts:
        return None
    return safe_parts


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


def _resolve_upload_destination(
    upload_dir: Path,
    filename: str,
    *,
    preserve_relative_path: bool,
) -> Path | None:
    """Return a safe destination for a multipart upload part."""
    if not preserve_relative_path:
        safe_name = _safe_upload_name(filename)
        return _next_available_path(upload_dir, safe_name) if safe_name else None

    safe_parts = _safe_upload_relative_parts(filename)
    if not safe_parts:
        return None

    parent = upload_dir.joinpath(*safe_parts[:-1])
    root_resolved = upload_dir.resolve()
    try:
        parent_resolved = parent.resolve(strict=False)
        parent_resolved.relative_to(root_resolved)
    except (OSError, ValueError):
        return None

    return _next_available_path(parent, safe_parts[-1])


_PROJECT_FILE_LIST_SKIP_DIRS = frozenset({".mira", "__pycache__", ".git", "node_modules"})
_PROJECT_FILE_LIST_LIMIT = 5000


def _collect_project_file_entries(
    project_dir: Path,
    limit: int = _PROJECT_FILE_LIST_LIMIT,
) -> tuple[list[dict[str, Any]], bool]:
    """Walk a project directory and return file rows for the UI explorer.

    Skipped directories are pruned rather than walked, symlinked directories are
    not followed, and files that resolve outside the project are left out because
    the artifacts endpoint would refuse to serve them. Returns the rows and
    whether *limit* cut the listing short.
    """
    root = project_dir.resolve()
    entries: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _PROJECT_FILE_LIST_SKIP_DIRS)
        current = Path(dirpath)
        for name in sorted(filenames):
            if name == ".DS_Store":
                continue
            path = current / name
            try:
                if path.is_symlink():
                    path.resolve().relative_to(root)
                stat = path.stat()
            except (OSError, ValueError):
                continue
            if len(entries) >= limit:
                return entries, True
            entries.append({
                "name": name,
                "path": path.relative_to(root).as_posix(),
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
                "is_dir": False,
            })
    return entries, False


def _attachment_disposition(filename: str) -> str:
    """Content-Disposition for a download, keeping non-ASCII names intact (RFC 6266)."""
    fallback = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in filename)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


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


_WS_CLOSE_TIMEOUT_S = 2.0
_HTTP_SHUTDOWN_TIMEOUT_S = 3.0
# Every method a route uses must be listed, or browsers block the request in
# the CORS preflight ("Load failed") before it reaches the handler.
_CORS_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"


class UiChannel(BaseChannel):
    """WebSocket + REST channel for frontend clients (desktop/browser UI)."""

    name = "ui"

    def __init__(
        self,
        config: UiChannelConfig,
        bus: MessageBus,
        workspace: Path | None = None,
        bind_host: str | None = None,
        bind_port: int | None = None,
        restrict_to_workspace: bool = True,
        on_runtime_config_updated: Callable[[Config, Path], Awaitable[None]] | None = None,
        auth_token: str | None = None,
    ):
        super().__init__(config, bus)
        self.config: UiChannelConfig = config
        self.workspace: Path | None = workspace
        legacy_host = getattr(config, "host", None)
        legacy_port = getattr(config, "port", None)
        self.bind_host: str = (
            bind_host
            or (legacy_host if isinstance(legacy_host, str) and legacy_host.strip() else "127.0.0.1")
        )
        self.bind_port: int = (
            bind_port if bind_port is not None else (legacy_port if isinstance(legacy_port, int) else 18790)
        )
        self.restrict_to_workspace: bool = restrict_to_workspace
        self._on_runtime_config_updated = on_runtime_config_updated
        # Resolved lazily when the app is built so constructing a channel never touches MIRA_HOME.
        self._configured_auth_token = auth_token
        self._auth_token: str | None = None
        storage_mode = getattr(config, "project_storage", "user_selectable")
        managed_root = getattr(config, "managed_project_root", None)
        from mira_engine.config.loader import get_home_dir

        default_root = (
            Path(managed_root)
            if storage_mode == "managed" and isinstance(managed_root, str) and managed_root.strip()
            else workspace or get_home_dir() / "workspace"
        )
        self.projects_root: Path = default_root.expanduser().resolve()
        self.project_registry = ProjectRegistry(
            self.projects_root,
            workspace_path=_resolve_project_workspace_path(),
            legacy_index_path=_resolve_project_dir_index_path(),
        )
        self._project_workspace_path: Path = self.project_registry.workspace_path
        self._project_dir_index_path: Path = self.project_registry.legacy_index_path
        self.organization_store = OrganizationStore(_resolve_organization_path())
        # Compatibility attributes used by existing tests and helper methods.
        # The registry owns the underlying mutable objects.
        self._project_dirs: dict[str, Path] = self.project_registry.project_dirs
        self._known_project_roots: set[Path] = self.project_registry.known_project_roots
        self._boot_ts: float = time.monotonic()
        # Snapshot the engine identity at boot so the desktop UI can detect
        # an in-place binary swap (DMG re-install) even before our process
        # exits. ``_current_engine_identity`` reads the on-disk manifest,
        # which the new bundle overwrites in place — re-reading it on every
        # ``/version`` would make the old, still-running engine appear to
        # match the new bundled identity and skip the reinstall flow.
        self._engine_identity: dict[str, Any] = _current_engine_identity()
        self._ui_instructions: str = _load_ui_instructions()
        self._clients: dict[str, web.WebSocketResponse] = {}
        # Every open WebSocket, bound to a session or not, so shutdown can close them all.
        self._sockets: set[web.WebSocketResponse] = set()
        # Sockets whose client understands turn_started/turn_finished; older
        # clients would render them as empty messages.
        self._turn_event_sockets: set[web.WebSocketResponse] = set()
        self._client_project_dirs: dict[str, Path | None] = {}
        # Accumulates streamed assistant text per session so the final turn can
        # be persisted to the UI history log on stream end.
        self._stream_buffers: dict[str, str] = {}
        # Turns the agent has started and not yet finished, per session and
        # workflow run, so a reconnecting client can drop a stale "thinking".
        self._active_turns: dict[str, dict[str, dict[str, Any]]] = {}
        self._app: web.Application | None = None
        self._workflow_api = WorkflowHttpApi(self.project_registry)
        self._knowledge = KnowledgeService(config=_knowledge_config, proxy=_web_proxy)
        self._knowledge_api = KnowledgeHttpApi(
            self._knowledge,
            audit=lambda action, details: self._audit(source="ui", action=action, details=details),
        )
        self._workflow_executor_id = uuid4().hex
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
                safe[key] = value if not isinstance(value, str) else UiChannel._preview(value, limit=500)
            else:
                safe[key] = UiChannel._preview(value, limit=500)
        return safe

    @staticmethod
    def _append_jsonl(path: Path, entry: dict[str, Any]) -> None:
        """Append one JSON line to path, creating parent dirs as needed."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _remember_projects_root(self, root: Path) -> Path:
        self._sync_project_registry()
        remembered = self.project_registry.remember_projects_root(root)
        self._known_project_roots = self.project_registry.known_project_roots
        return remembered

    def _sync_project_registry(self) -> None:
        """Keep legacy mutable UI attributes aligned with the shared registry."""

        root = self.projects_root.expanduser().resolve()
        if self.project_registry.projects_root != root:
            self.project_registry.projects_root = root
        if self._known_project_roots is not self.project_registry.known_project_roots:
            self.project_registry._known_project_roots = {
                Path(p).expanduser().resolve() for p in self._known_project_roots
            }
            self._known_project_roots = self.project_registry.known_project_roots
        if self._project_dirs is not self.project_registry.project_dirs:
            self.project_registry._project_dirs = dict(self._project_dirs)
            self._project_dirs = self.project_registry.project_dirs

    def _load_project_dir_index(self) -> None:
        return None

    def _save_project_dir_index(self) -> None:
        self.project_registry.save()
        self.project_registry.save_legacy_index()

    def _register_project_dir(
        self,
        session_id: str,
        project_dir: Path,
        *,
        persist: bool = True,
    ) -> Path:
        return self.project_registry.register_project_dir(
            session_id,
            project_dir,
            persist=persist,
        ).project_dir

    def _drop_project_dir_registration(
        self, session_id: str, *, persist: bool = True, hide: bool = False
    ) -> None:
        self.project_registry.drop_project_dir_registration(
            session_id,
            persist=persist,
            hide=hide,
        )

    def _register_projects_under_root(self, root: Path) -> None:
        self.project_registry.register_projects_under_root(root)

    def _resolve_project_dir(
        self,
        session_id: str,
        *,
        create: bool = False,
    ) -> Path | None:
        session_key = session_id.strip()
        if not session_key:
            return None
        self._sync_project_registry()
        try:
            return self.project_registry.resolve(
                session_key,
                create=create,
            ).project_dir
        except (FileNotFoundError, ValueError):
            return None

    def _project_dir_from_metadata(
        self,
        session_id: str | None,
        metadata: dict[str, Any],
    ) -> Path | None:
        if not session_id:
            return None
        raw_project_dir = metadata.get("project_dir")
        if not isinstance(raw_project_dir, str) or not raw_project_dir.strip():
            return None
        try:
            project_dir = Path(raw_project_dir).expanduser().resolve()
        except OSError:
            return None
        if not project_dir.is_dir():
            return None
        try:
            return self._register_project_dir(session_id, project_dir)
        except ValueError:
            return project_dir

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
        resolved_project_dir = project_dir
        if resolved_project_dir is None and session_id:
            resolved_project_dir = self._resolve_project_dir(session_id)
        if project_dir is not None:
            entry["project_dir"] = str(project_dir)
        elif resolved_project_dir is not None:
            entry["project_dir"] = str(resolved_project_dir)

        try:
            global_log = self.projects_root / "logs" / _GLOBAL_AUDIT_FILENAME
            self._append_jsonl(global_log, entry)
        except OSError as exc:
            logger.warning("Failed to append global audit log: {}", exc)

        target = resolved_project_dir
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

    def _free_port_from_stale_engine(self) -> None:
        """Before binding, stop a leftover engine for this MIRA_HOME that still holds our port.

        Raises :class:`~mira_engine.cli.port_guard.PortInUseError` when anything
        else holds it; such a process is never signalled.
        """
        from mira_engine.cli.port_guard import free_port_from_stale_engine

        free_port_from_stale_engine(self.bind_host, self.bind_port)

    def _build_app(self) -> web.Application:
        from mira_engine.observability import request_middleware

        if self._auth_token is None:
            self._auth_token = resolve_gateway_token(self._configured_auth_token)
        # CORS outermost so 401/403 stay readable by browser clients; auth innermost so
        # rejected requests are still logged (by route template, never by query string).
        self._app = web.Application(
            middlewares=[self._cors_middleware, request_middleware(), self._auth_middleware],
        )
        self._app.router.add_get("/ws", self._ws_handler)
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/version", self._handle_version)
        self._app.router.add_get("/api/health", self._handle_health)
        self._app.router.add_get("/api/version", self._handle_version)
        self._app.router.add_get("/api/status", self._handle_status)
        self._app.router.add_get("/api/sessions", self._handle_sessions)
        self._app.router.add_get("/api/sessions/{session_id}/history", self._handle_history)
        self._app.router.add_get("/api/sessions/{session_id}/skills", self._handle_skill_history)
        self._app.router.add_get("/api/sessions/{session_id}/usage", self._handle_token_usage)
        self._app.router.add_get("/api/sessions/{session_id}/turns", self._handle_active_turns)
        self._app.router.add_get(
            "/api/sessions/{session_id}/context-preview",
            self._handle_chat_context_preview,
        )
        self._app.router.add_post(
            "/api/sessions/{session_id}/context-preview",
            self._handle_chat_context_preview,
        )
        self._app.router.add_get("/api/plan", self._handle_plan)
        self._app.router.add_get("/api/plan/contract", self._handle_plan_contract)
        self._app.router.add_get("/api/plan/lint", self._handle_plan_lint)
        self._app.router.add_get("/api/config", self._handle_get_config)
        self._app.router.add_post("/api/config", self._handle_config)
        self._app.router.add_get("/api/profile", self._handle_get_profile)
        self._app.router.add_post("/api/profile", self._handle_save_profile)
        self._app.router.add_get("/api/providers/{name}/models", self._handle_provider_models)
        self._app.router.add_post("/api/providers/{name}/test", self._handle_provider_test)
        self._app.router.add_put("/api/team/layer", self._handle_team_layer)
        self._app.router.add_get("/api/feedback/config", self._handle_feedback_config)
        self._app.router.add_post("/api/feedback", self._handle_feedback)
        self._app.router.add_get("/api/community/status", self._handle_community_status)
        self._app.router.add_post("/api/community/autonomy", self._handle_community_autonomy)
        self._app.router.add_get("/api/community/approvals", self._handle_community_approvals)
        self._app.router.add_get("/api/community/inbox", self._handle_community_inbox)
        self._app.router.add_post("/api/community/inbox/read", self._handle_community_inbox_read)
        self._app.router.add_put("/api/community/notifications", self._handle_community_notifications)
        self._app.router.add_get("/api/community/profile", self._handle_community_profile)
        self._app.router.add_put("/api/community/profile", self._handle_community_profile_update)
        self._app.router.add_post(
            "/api/community/approvals/{approval_id}/decide", self._handle_community_decide
        )
        self._app.router.add_post("/api/community/pair/start", self._handle_community_pair_start)
        self._app.router.add_post("/api/community/pair/poll", self._handle_community_pair_poll)
        self._app.router.add_post("/api/community/onboard", self._handle_community_onboard)
        self._app.router.add_post(
            "/api/community/proposals/{proposal_id}/renew", self._handle_community_renew_claim
        )
        self._app.router.add_get("/api/projects", self._handle_list_projects)
        self._app.router.add_post("/api/projects", self._handle_create_project)
        self._app.router.add_get("/api/organization", self._handle_organization)
        self._app.router.add_post("/api/organization/folders", self._handle_create_folder)
        self._app.router.add_patch("/api/organization/folders/{folder_id}", self._handle_rename_folder)
        self._app.router.add_delete("/api/organization/folders/{folder_id}", self._handle_delete_folder)
        self._app.router.add_patch(
            "/api/organization/items/{kind}/{item_id}/folder",
            self._handle_assign_folder,
        )
        self._app.router.add_post("/api/organization/chats/import", self._handle_import_chats)
        self._app.router.add_patch("/api/organization/chats/{chat_id}", self._handle_update_chat)
        self._app.router.add_delete("/api/organization/chats/{chat_id}", self._handle_delete_chat)
        self._app.router.add_post("/api/data-path/validate", self._handle_validate_data_path)
        self._app.router.add_patch("/api/projects/{session_id}/meta", self._handle_project_meta)
        self._app.router.add_post("/api/projects/{session_id}/remove", self._handle_remove_project)
        self._app.router.add_delete("/api/projects", self._handle_delete_project)
        self._app.router.add_get("/api/projects/{session_id}/files", self._handle_list_project_files)
        self._app.router.add_post("/api/projects/{session_id}/files", self._handle_upload_project_files)
        self._app.router.add_post(
            "/api/projects/{session_id}/context/from-chat",
            self._handle_attach_chat_context,
        )
        self._app.router.add_get("/api/projects/{session_id}/artifacts", self._handle_project_artifact)
        self._app.router.add_get("/api/projects/{session_id}/skill-plugins", self._handle_skill_plugins_list)
        self._app.router.add_post("/api/projects/{session_id}/skill-plugins/install", self._handle_skill_plugins_install)
        self._app.router.add_post("/api/projects/{session_id}/skill-plugins/state", self._handle_skill_plugins_state)
        self._app.router.add_delete("/api/projects/{session_id}/skill-plugins/{plugin_id}", self._handle_skill_plugins_uninstall)
        self._workflow_api.add_routes(self._app)
        if _knowledge_config().enabled:
            self._knowledge_api.add_routes(self._app)
            self._app.router.add_get("/api/projects/{session_id}/knowledge", self._handle_project_knowledge)
            self._app.router.add_put("/api/projects/{session_id}/knowledge", self._handle_project_knowledge)
        return self._app

    async def start(self) -> None:
        self._free_port_from_stale_engine()
        self._build_app()
        if _knowledge_config().enabled:
            self._knowledge.start()  # resume sources interrupted by the last shutdown

        # The access log would record request lines, including ?access_token=.
        # A short shutdown_timeout keeps in-flight requests from stalling engine shutdown.
        self._runner = web.AppRunner(self._app, access_log=None, shutdown_timeout=_HTTP_SHUTDOWN_TIMEOUT_S)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner, self.bind_host, self.bind_port,
            reuse_address=True,
        )
        await self._site.start()
        self._running = True
        logger.info(
            "UI channel listening on {}:{} (WebSocket + HTTP)",
            self.bind_host,
            self.bind_port,
        )
        warn_if_exposed(self.bind_host, self._allowed_origins())

        # Keep the channel alive until stopped
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        self._running = False

        # Stop accepting connections first, then tell clients the engine is going away.
        if self._site:
            await self._site.stop()
            self._site = None
        sockets = {*self._sockets, *self._clients.values()}
        self._clients.clear()
        self._client_project_dirs.clear()
        await self._close_sockets(sockets, code=WSCloseCode.GOING_AWAY, message=b"engine shutting down")
        await self._workflow_api.close()
        await self._knowledge.close()

        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        logger.info("UI channel stopped")

    async def _close_active_clients(self) -> None:
        clients = set(self._clients.values())
        self._clients.clear()
        self._client_project_dirs.clear()
        await self._close_sockets(clients)

    @staticmethod
    async def _close_sockets(
        sockets: set[web.WebSocketResponse], *, code: int = WSCloseCode.OK, message: bytes = b"",
    ) -> None:
        if not sockets:
            return
        # Close in parallel and bounded: an unresponsive client must not hold up the rest.
        closing = asyncio.gather(
            *(ws.close(code=code, message=message) for ws in sockets), return_exceptions=True,
        )
        try:
            await asyncio.wait_for(closing, _WS_CLOSE_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning("Some WebSocket clients did not acknowledge close within {}s", _WS_CLOSE_TIMEOUT_S)

    def _stream_history_dir(
        self, chat_id: str, metadata: dict[str, Any]
    ) -> Path | None:
        """Resolve the session-log directory for a streamed turn (project dir
        when available, else the workspace-level log for Quick Chat)."""
        project_dir = self._project_dir_from_metadata(chat_id, metadata)
        if project_dir is None:
            project_dir = self._resolve_project_dir(chat_id)
        if project_dir is not None and project_dir.is_dir():
            return project_dir
        if self.workspace is not None:
            return self.workspace
        return None

    @staticmethod
    def _ui_history_key(chat_id: str, metadata: dict[str, Any]) -> str:
        run_id = metadata.get("workflow_run_id")
        if isinstance(run_id, str) and run_id:
            return f"ui:{chat_id}:run:{run_id}"
        return f"ui:{chat_id}"

    # Inbound metadata is echoed on every outbound frame. These keys are
    # engine-internal prompt material (the UI instructions blob alone is
    # ~18 KB) and must not ride along on each token or into the session log.
    _WIRE_METADATA_DROP: frozenset[str] = frozenset({"_ui_system_instructions"})

    @classmethod
    def _wire_metadata(cls, metadata: dict[str, Any]) -> dict[str, Any]:
        if not any(key in metadata for key in cls._WIRE_METADATA_DROP):
            return metadata
        return {k: v for k, v in metadata.items() if k not in cls._WIRE_METADATA_DROP}

    async def send(self, msg: OutboundMessage) -> None:
        metadata = msg.metadata or {}
        if metadata.get("_turn_event"):
            await self._send_turn_event(msg)
            return
        if metadata.get("_usage_only"):
            # Do not append a chat message, toggle streaming, or refresh workflow state.
            ws = self._clients.get(msg.chat_id)
            if ws is not None and not ws.closed:
                try:
                    await ws.send_json({"type": "token_usage", "session_id": msg.chat_id,
                                        "content": "", "metadata": {"token_usage": metadata.get("token_usage")}})
                except Exception:
                    logger.debug("Token usage delivery deferred to snapshot")
            return

        workflow_run_id = metadata.get("workflow_run_id")
        workflow_project_id = metadata.get("project_id")
        if (
            isinstance(workflow_run_id, str)
            and isinstance(workflow_project_id, str)
            and not metadata.get("_audit_only")
        ):
            if (
                metadata.get("_activity_ping")
                or metadata.get("_tool_hint")
                or metadata.get("_progress")
            ):
                await self._workflow_api.broadcast_activity(
                    workflow_project_id,
                    workflow_run_id,
                    msg.content,
                )
            elif metadata.get("_stream_end") or not metadata.get("_stream_delta"):
                await self._workflow_api.broadcast_activity(
                    workflow_project_id,
                    workflow_run_id,
                    "",
                )
        if metadata.get("_audit_only"):
            action = metadata.get("_audit_event")
            details = metadata.get("_audit_details")
            if action == "skill_loaded" and isinstance(details, dict):
                details = {
                    **details,
                    "turn_id": metadata.get("turn_id"),
                    "workflow_run_id": metadata.get("workflow_run_id") or details.get("run_id"),
                    "module_type": metadata.get("workflow_module_type") or details.get("module_type"),
                }
            if isinstance(action, str) and action:
                project_dir = self._project_dir_from_metadata(msg.chat_id, metadata)
                self._audit(
                    source="agent",
                    action=action,
                    session_id=msg.chat_id,
                    project_dir=project_dir,
                    details=details if isinstance(details, dict) else {},
                )
            if action == "skill_loaded" and (event := self._skill_history_event(details)):
                ws = self._clients.get(msg.chat_id)
                if ws is not None and not ws.closed:
                    try:
                        await ws.send_json({
                            "type": "skill_loaded", "session_id": msg.chat_id, "content": "",
                            "metadata": {"skill_event": event},
                        })
                    except Exception as exc:
                        logger.debug("Skill event delivery deferred to history: {}", exc)
            return

        metadata = self._wire_metadata(msg.metadata or {})

        # A provider retry or model fallback restarted generation: withdraw the
        # partial reply (both the client's live bubble and our pending buffer).
        if metadata.get("_stream_reset"):
            if msg.chat_id:
                self._stream_buffers.pop(msg.chat_id, None)
                ws = self._clients.get(msg.chat_id)
                if ws is not None and not ws.closed:
                    try:
                        await ws.send_json({
                            "type": "stream_reset",
                            "session_id": msg.chat_id,
                            "content": "",
                            "metadata": metadata,
                        })
                    except Exception as e:  # noqa: BLE001
                        logger.warning("Failed to send stream reset to {}: {}", msg.chat_id, e)
            return

        # Streaming token deltas: forward to the client live and accumulate so
        # the completed turn can be persisted on stream end. Deltas/end markers
        # are never written to the history log individually.
        if metadata.get("_stream_delta"):
            if msg.chat_id and msg.content:
                self._stream_buffers[msg.chat_id] = (
                    self._stream_buffers.get(msg.chat_id, "") + msg.content
                )
                ws = self._clients.get(msg.chat_id)
                if ws is not None and not ws.closed:
                    try:
                        await ws.send_json({
                            "type": "stream_delta",
                            "session_id": msg.chat_id,
                            "content": msg.content,
                            "metadata": metadata,
                        })
                    except Exception as e:  # noqa: BLE001
                        logger.warning("Failed to stream delta to {}: {}", msg.chat_id, e)
            return

        if metadata.get("_stream_end"):
            joined = self._stream_buffers.pop(msg.chat_id, "") if msg.chat_id else ""
            if joined and msg.chat_id:
                history_dir = self._stream_history_dir(msg.chat_id, metadata)
                if history_dir is not None:
                    SessionManager(history_dir).append_ui_event(
                        key=self._ui_history_key(msg.chat_id, metadata),
                        role="assistant",
                        content=joined,
                        msg_type="response",
                        metadata={k: v for k, v in metadata.items() if not k.startswith("_stream")},
                    )
            ws = self._clients.get(msg.chat_id)
            if ws is not None and not ws.closed:
                try:
                    await ws.send_json({
                        "type": "stream_end",
                        "session_id": msg.chat_id,
                        "content": "",
                        "metadata": metadata,
                    })
                except Exception as e:  # noqa: BLE001
                    logger.warning("Failed to send stream end to {}: {}", msg.chat_id, e)
            return

        project_dir = (
            self._project_dir_from_metadata(msg.chat_id, metadata)
            if msg.chat_id
            else None
        )
        if project_dir is None:
            project_dir = self._resolve_project_dir(msg.chat_id) if msg.chat_id else None
        is_progress = metadata.get("_progress", False)
        is_activity_ping = bool(metadata.get("_activity_ping", False))
        is_error = bool(metadata.get("_error", False))
        is_stop_ack = bool(metadata.get("_stop_ack", False))
        is_skill_fallback = bool(metadata.get("_skill_fallback_required", False))
        msg_type = (
            "stop_ack" if is_stop_ack
            else "skill_fallback_required" if is_skill_fallback
            else "error" if is_error
            else "progress" if is_progress
            else "response"
        )
        if is_stop_ack and msg.chat_id:
            self._stream_buffers.pop(msg.chat_id, None)
        common_details = {
            "type": msg_type,
            "tool_hint": bool(metadata.get("_tool_hint", False)),
            "content_preview": self._preview(msg.content),
        }
        history_dir: Path | None = (
            project_dir if (project_dir and project_dir.is_dir()) else None
        )
        if history_dir is None and self.workspace is not None and msg.chat_id:
            # Quick-chat (no project): persist assistant turns under the
            # workspace-level session log so the chat history reloads.
            history_dir = self.workspace
        if history_dir is not None and not is_activity_ping and not is_stop_ack:
            SessionManager(history_dir).append_ui_event(
                key=self._ui_history_key(msg.chat_id, metadata),
                role="assistant",
                content=msg.content,
                msg_type=msg_type,
                metadata=metadata,
            )
        ws = self._clients.get(msg.chat_id)
        if ws is None or ws.closed:
            self._audit(
                source="agent",
                action="ws_outbound_dropped",
                session_id=msg.chat_id,
                project_dir=project_dir,
                details={**common_details, "reason": "no_active_client"},
            )
            logger.debug("No active WebSocket for chat_id={}", msg.chat_id)
            return
        bound_project_dir = self._client_project_dirs.get(msg.chat_id)
        if (
            project_dir is not None
            and bound_project_dir is not None
            and project_dir != bound_project_dir
        ):
            self._audit(
                source="agent",
                action="ws_outbound_dropped",
                session_id=msg.chat_id,
                project_dir=project_dir,
                details={
                    **common_details,
                    "reason": "client_bound_to_different_project",
                    "bound_project_dir": str(bound_project_dir),
                },
            )
            logger.debug(
                "Dropped UI outbound for {} from {} because client is bound to {}",
                msg.chat_id,
                project_dir,
                bound_project_dir,
            )
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
                project_dir=project_dir,
                details=common_details,
            )
        except Exception as e:
            self._audit(
                source="agent",
                action="ws_outbound_failed",
                session_id=msg.chat_id,
                project_dir=project_dir,
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
        project_dir = self._resolve_project_dir(session_id)
        if project_dir is None:
            return None
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

    def _persist_plan_patch(self, project_dir: Path, patch: dict[str, Any]) -> None:
        """Merge ``patch`` into the task_plan.json ``plan`` block, creating it if needed.

        Used to record interactive plan answers / revision feedback coming from
        the UI before the agent is re-triggered to act on them.
        """
        plan_path = project_dir / PLAN_FILENAME
        try:
            if plan_path.is_file():
                data = json.loads(plan_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            else:
                data = {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read {} for plan patch: {}", plan_path, exc)
            return
        block = data.get("plan")
        if not isinstance(block, dict):
            block = {}
        block.update(patch)
        data["plan"] = block
        if not isinstance(data.get("schema_version"), int):
            data["schema_version"] = 1
        try:
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Failed to write plan patch to {}: {}", plan_path, exc)

    # ── CORS / auth middleware ───────────────────────────────────────

    def _allowed_origins(self) -> list[str]:
        return allowed_origins(getattr(self.config, "cors_origins", None))

    @web.middleware
    async def _auth_middleware(
        self,
        request: web.Request,
        handler: Any,
    ) -> web.StreamResponse:
        """Reject foreign browser origins and requests without the gateway token."""
        if request.method == "OPTIONS" or (
            request.method in {"GET", "HEAD"} and request.path in GATEWAY_EXEMPT_PATHS
        ):
            return await handler(request)
        if not origin_allowed(request.headers.get("Origin"), self._allowed_origins()):
            return web.json_response({"error": "origin not allowed"}, status=403)
        if not token_matches(self._auth_token or "", presented_token(request)):
            return web.json_response(
                {"error": "unauthorized"}, status=401, headers={"WWW-Authenticate": "Bearer"},
            )
        return await handler(request)

    @web.middleware
    async def _cors_middleware(
        self,
        request: web.Request,
        handler: Any,
    ) -> web.StreamResponse:
        if request.method == "OPTIONS":
            resp = web.Response(status=204)
        else:
            try:
                resp = await handler(request)
            except web.HTTPException as exc:
                # aiohttp raises unmatched-route and method errors before a
                # handler can build a response. Keep CORS headers on those
                # responses so browser clients can inspect the HTTP status.
                resp = exc

        origin = request.headers.get("Origin", "*")
        allowed = self._allowed_origins()
        if "*" in allowed:
            resp.headers["Access-Control-Allow-Origin"] = origin
        elif origin in allowed:
            resp.headers["Access-Control-Allow-Origin"] = origin

        resp.headers["Access-Control-Allow-Methods"] = _CORS_METHODS
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Mira-Request-Id"
        resp.headers["Access-Control-Expose-Headers"] = "X-Mira-Request-Id"
        return resp

    # ── WebSocket handler ────────────────────────────────────────────

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        from mira_engine.observability import correlation_id, emit_event

        connection_id = correlation_id()
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        emit_event("websocket.connected", connection_id=connection_id)
        self._sockets.add(ws)
        if request.query.get("turn_events") == "1":
            self._turn_event_sockets.add(ws)

        session_id: str | None = None

        try:
            async for raw in ws:
                if raw.type != web.WSMsgType.TEXT:
                    continue

                try:
                    data: dict = json.loads(raw.data)
                except (json.JSONDecodeError, TypeError, RecursionError):
                    await ws.send_json({"type": "error", "content": "Invalid JSON"})
                    continue
                if not isinstance(data, dict):
                    await ws.send_json({"type": "error", "content": "Invalid JSON"})
                    continue

                msg_type = data.get("type")
                data["request_id"] = correlation_id(data.get("request_id"))
                emit_event("websocket.received", connection_id=connection_id,
                           request_id=data["request_id"], operation=msg_type if isinstance(msg_type, str) and msg_type in {
                               "stop", "skill_fallback_response", "message", "user_input", "chat", "bind", "plan_decision", "plan_answers",
                           } else "unknown")

                if msg_type == "stop":
                    session_id = data.get("session_id", session_id)
                    if session_id is None:
                        await ws.send_json({"type": "error", "content": "session_id required"})
                        continue
                    loop_mode = _normalize_loop_mode(data.get("loop_mode"))
                    project_dir_path = (
                        self._resolve_project_dir(session_id, create=False)
                        if loop_mode == "project"
                        else None
                    )
                    self._clients[session_id] = ws
                    self._client_project_dirs[session_id] = project_dir_path
                    metadata: dict[str, Any] = {
                        "source": "ui",
                        "loop_mode": loop_mode,
                        "_stop_request_id": data.get("request_id"),
                        "turn_id": data.get("turn_id"),
                    }
                    if project_dir_path is not None:
                        metadata.update({
                            "project_id": session_id,
                            "project_dir": str(project_dir_path),
                        })
                    await self._handle_message(
                        sender_id=data.get("user_id", "ui_user"),
                        chat_id=session_id,
                        content="/stop",
                        media=[],
                        metadata={**metadata, "_diagnostic_request_id": data["request_id"]},
                        session_key=f"ui:{session_id}",
                    )
                elif msg_type == "skill_fallback_response":
                    session_id = data.get("session_id", session_id)
                    if session_id is None:
                        await ws.send_json({"type": "error", "content": "session_id required"})
                        continue
                    approved = bool(data.get("approved"))
                    selected_skill_ids = data.get("selected_skill_ids")
                    if not isinstance(selected_skill_ids, list):
                        selected_skill_ids = []
                    selected_skill_ids = [
                        str(item).strip() for item in selected_skill_ids
                        if isinstance(item, str) and str(item).strip()
                    ][:8]
                    if not approved:
                        self._audit(
                            source="ui",
                            action="skill_fallback_rejected",
                            session_id=session_id,
                            project_dir=self._resolve_project_dir(session_id),
                            details={"selected_skill_ids": selected_skill_ids},
                        )
                        await ws.send_json({
                            "type": "response",
                            "session_id": session_id,
                            "content": "Skill fallback cancelled.",
                            "metadata": {"_skill_fallback_cancelled": True},
                        })
                        continue
                    loop_mode = _normalize_loop_mode(data.get("loop_mode"))
                    project_dir_path = (
                        self._resolve_project_dir(session_id, create=False)
                        if loop_mode == "project"
                        else None
                    )
                    self._clients[session_id] = ws
                    self._client_project_dirs[session_id] = project_dir_path
                    self._audit(
                        source="ui",
                        action="skill_fallback_approved",
                        session_id=session_id,
                        project_dir=project_dir_path,
                        details={"selected_skill_ids": selected_skill_ids},
                    )
                    metadata = {
                        "source": "ui",
                        "loop_mode": loop_mode,
                        "turn_id": data.get("turn_id"),
                        "selected_skill_ids": selected_skill_ids,
                        "_skill_fallback_approved": True,
                        "_wants_stream": True,
                    }
                    if project_dir_path is not None:
                        metadata.update({
                            "project_id": session_id,
                            "project_dir": str(project_dir_path),
                        })
                    await self._handle_message(
                        sender_id=data.get("user_id", "ui_user"),
                        chat_id=session_id,
                        content="The user approved the proposed Skill fallback. Continue only with that fallback.",
                        media=[],
                        metadata={**metadata, "_diagnostic_request_id": data["request_id"]},
                        session_key=f"ui:{session_id}",
                    )
                elif msg_type == "message":
                    session_id = data.get("session_id", session_id)
                    user_id = data.get("user_id", session_id or "anonymous")
                    content = data.get("content", "")
                    media = data.get("media", [])
                    loop_mode = _normalize_loop_mode(data.get("loop_mode"))
                    run_mode = _normalize_run_mode(data.get("mode"))
                    agent_profile = _normalize_agent_profile(data.get("agent_profile"))
                    contract_version = (
                        _normalize_contract_version(data.get("contract_version"))
                        if "contract_version" in data
                        else None
                    )
                    incoming_policy = _normalize_automation_policy(data.get("automation_policy"))
                    workflow_run_id = data.get("workflow_run_id")
                    workflow_run = None
                    allow_result_write = bool(data.get("allow_result_write"))
                    turn_id = data.get("turn_id")
                    selected_skill_ids = data.get("selected_skill_ids")
                    if not isinstance(selected_skill_ids, list):
                        selected_skill_ids = []
                    selected_skill_ids = [
                        str(item).strip() for item in selected_skill_ids
                        if isinstance(item, str) and str(item).strip()
                    ][:8]
                    # Token streaming is opt-in per message. Default on so clients
                    # that omit the flag still get a responsive experience.
                    stream_pref = data.get("stream")
                    wants_stream = True if stream_pref is None else bool(stream_pref)

                    if session_id is None:
                        await ws.send_json(
                            {"type": "error", "content": "session_id required"}
                        )
                        continue

                    project_dir_path: Path | None = None
                    project_dir: str | None = None
                    meta: dict[str, Any] = {
                        "contract_version": PROJECT_META_DEFAULT_CONTRACT_VERSION,
                        "automation_policy": None,
                    }
                    if loop_mode == "project":
                        project_dir_path = self._resolve_project_dir(
                            session_id, create=True
                        )
                        if project_dir_path is None:
                            await ws.send_json(
                                {"type": "error", "content": "project_dir resolution failed"}
                            )
                            continue
                        project_dir = str(project_dir_path)
                        meta = self._persist_project_runtime_preferences(
                            project_dir_path,
                            run_mode=run_mode,
                            agent_profile=agent_profile,
                            contract_version=contract_version,
                            automation_policy=incoming_policy,
                        )
                        if workflow_run_id is not None:
                            if not isinstance(workflow_run_id, str) or not workflow_run_id:
                                await ws.send_json({"type": "error", "content": "invalid workflow_run_id"})
                                continue
                            try:
                                workflow_service = self._workflow_api._service(session_id)
                                workflow_run = workflow_service.repository.load_run(workflow_run_id)
                                claim_receipt = workflow_service.claim_run(
                                    workflow_run_id,
                                    lease_owner=f"ui:{self._workflow_executor_id}:{user_id}",
                                    agent_profile=agent_profile,
                                )
                                workflow_run = workflow_service.repository.load_run(
                                    workflow_run_id
                                )
                                if (
                                    claim_receipt
                                    and claim_receipt.event
                                    and claim_receipt.event.event_type
                                    == "run.recovery_required"
                                ):
                                    await ws.send_json({
                                        "type": "error",
                                        "content": (
                                            "MIRA restarted while this module was running. "
                                            "The Run is paused to avoid repeating uncertain work; "
                                            "review it and choose Resume to continue."
                                        ),
                                    })
                                    continue
                            except Exception as exc:
                                await ws.send_json({"type": "error", "content": str(exc)})
                                continue
                    elif workflow_run_id is not None:
                        await ws.send_json({
                            "type": "error",
                            "content": "workflow_run_id is valid only in project mode",
                        })
                        continue
                    self._clients[session_id] = ws
                    self._client_project_dirs[session_id] = project_dir_path
                    effective_policy = _normalize_automation_policy(meta.get("automation_policy"))
                    plan_ids_before = (
                        _extract_plan_experiment_ids(project_dir_path)
                        if project_dir_path and workflow_run is None
                        else []
                    )
                    guard = (
                        guard_task_plan_file(project_dir_path, auto_fix=True)
                        if project_dir_path and workflow_run is None
                        else {"fixed": False, "blocking": False}
                    )
                    guard_notice: str | None = None
                    if guard.get("fixed"):
                        plan_ids_after = _extract_plan_experiment_ids(project_dir_path) if project_dir_path else []
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
                            "loop_mode": loop_mode,
                            "run_mode": run_mode,
                            "agent_profile": agent_profile,
                            "contract_version": _normalize_contract_version(
                                meta.get("contract_version")
                            ),
                            "has_automation_policy": bool(effective_policy),
                            "goal_count": len(effective_policy.get("goals", [])) if effective_policy else 0,
                            "allow_result_write": allow_result_write,
                            "content_preview": self._preview(content),
                            "media_count": len(media) if isinstance(media, list) else 0,
                        },
                    )
                    if project_dir_path:
                        SessionManager(project_dir_path).append_ui_event(
                            key=(
                                f"ui:{session_id}:run:{workflow_run_id}"
                                if workflow_run is not None
                                else f"ui:{session_id}"
                            ),
                            role="user",
                            content=content,
                            msg_type="response",
                            metadata={"_user": True},
                        )
                    elif loop_mode == "normal" and self.workspace is not None:
                        # Quick-chat sessions have no project dir; persist the user
                        # turn under the workspace-level session log so history loads.
                        SessionManager(self.workspace).append_ui_event(
                            key=f"ui:{session_id}",
                            role="user",
                            content=content,
                            msg_type="response",
                            metadata={"_user": True},
                        )
                    metadata: dict[str, Any] = {
                        "source": "ui",
                        "loop_mode": loop_mode,
                        "run_mode": run_mode,
                        "agent_profile": agent_profile,
                        "contract_version": _normalize_contract_version(
                            meta.get("contract_version")
                        ),
                        "_allow_result_write": allow_result_write,
                        "turn_id": turn_id,
                        "selected_skill_ids": selected_skill_ids,
                    }
                    if wants_stream:
                        metadata["_wants_stream"] = True
                    if isinstance(data.get("selected_kb_ids"), list):
                        # An explicit choice (even empty) overrides the project's bases.
                        metadata["selected_kb_ids"] = clean_kb_ids(data["selected_kb_ids"])
                    if project_dir is not None:
                        metadata["project_id"] = session_id
                        metadata["project_dir"] = project_dir
                    if workflow_run is not None:
                        metadata["workflow_run_id"] = workflow_run_id
                        metadata["workflow_module_type"] = workflow_run.module_type
                        # The legacy research loop's auto mode assumes one mandatory
                        # four-stage task_plan. ModuleRuns control their own lifecycle.
                        metadata["run_mode"] = "manual"
                    if effective_policy:
                        metadata["automation_policy"] = effective_policy
                    if loop_mode == "project":
                        if workflow_run is not None:
                            instruction_parts = [
                                module_system_instructions(
                                    workflow_run.module_type, workflow_run_id
                                )
                            ]
                            interaction = workflow_run.module_data.get("interaction")
                            if (
                                isinstance(interaction, dict)
                                and interaction.get("response") is not None
                            ):
                                instruction_parts.append(
                                    "The latest structured interaction has been resolved. "
                                    "Continue from this authoritative response:\n"
                                    + json.dumps(
                                        {
                                            "interaction_id": interaction.get("interaction_id"),
                                            "interaction_type": interaction.get("interaction_type"),
                                            "response": interaction.get("response"),
                                        },
                                        ensure_ascii=False,
                                    )
                                )
                        else:
                            instruction_parts = (
                                [self._ui_instructions] if self._ui_instructions else []
                            )
                        if instruction_parts:
                            metadata["_ui_system_instructions"] = "\n\n".join(instruction_parts)
                    if guard_notice:
                        metadata["_task_plan_guard_notice"] = guard_notice
                    await self._handle_message(
                        sender_id=user_id,
                        chat_id=session_id,
                        content=content,
                        media=media,
                        metadata={**metadata, "_diagnostic_request_id": data["request_id"]},
                        session_key=(
                            f"ui:{session_id}:run:{workflow_run_id}"
                            if workflow_run is not None
                            else f"ui:{session_id}"
                        ),
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

                    project_dir_path = self._resolve_project_dir(
                        session_id, create=True
                    )
                    if project_dir_path is None:
                        await ws.send_json(
                            {"type": "error", "content": "project_dir resolution failed"}
                        )
                        continue
                    self._clients[session_id] = ws
                    self._client_project_dirs[session_id] = project_dir_path
                    project_dir = str(project_dir_path)
                    self._audit(
                        source="ui",
                        action="ws_set_mode_received",
                        session_id=session_id,
                        project_dir=project_dir_path,
                        details={
                            "user_id": user_id,
                            "run_mode": run_mode,
                        },
                    )
                    metadata = {
                        "source": "ui",
                        "project_id": session_id,
                        "project_dir": project_dir,
                        "run_mode": run_mode,
                        "_control": "set_mode",
                    }
                    await self._handle_message(
                        sender_id=user_id,
                        chat_id=session_id,
                        content="__set_mode__",
                        media=[],
                        metadata={**metadata, "_diagnostic_request_id": data["request_id"]},
                        session_key=f"ui:{session_id}",
                    )
                elif msg_type == "plan_answer":
                    session_id = data.get("session_id", session_id)
                    user_id = data.get("user_id", session_id or "anonymous")
                    answers = data.get("answers")
                    if not isinstance(answers, dict):
                        answers = {}
                    if session_id is None:
                        await ws.send_json(
                            {"type": "error", "content": "session_id required"}
                        )
                        continue
                    project_dir_path = self._resolve_project_dir(session_id, create=True)
                    if project_dir_path is None:
                        await ws.send_json(
                            {"type": "error", "content": "project_dir resolution failed"}
                        )
                        continue
                    self._clients[session_id] = ws
                    self._client_project_dirs[session_id] = project_dir_path
                    project_dir = str(project_dir_path)
                    run_mode = _normalize_run_mode(data.get("mode"))
                    agent_profile = _normalize_agent_profile(data.get("agent_profile"))
                    meta = self._persist_project_runtime_preferences(
                        project_dir_path,
                        run_mode=run_mode,
                        agent_profile=agent_profile,
                        contract_version=None,
                        automation_policy=_normalize_automation_policy(
                            data.get("automation_policy")
                        ),
                    )
                    effective_policy = _normalize_automation_policy(meta.get("automation_policy"))
                    self._persist_plan_patch(project_dir_path, {"answers": answers})
                    self._audit(
                        source="ui",
                        action="ws_plan_answer_received",
                        session_id=session_id,
                        project_dir=project_dir_path,
                        details={"user_id": user_id, "answer_count": len(answers)},
                    )
                    metadata = {
                        "source": "ui",
                        "loop_mode": "project",
                        "run_mode": run_mode,
                        "agent_profile": agent_profile,
                        "contract_version": _normalize_contract_version(
                            meta.get("contract_version")
                        ),
                        "project_dir": project_dir,
                        "_wants_stream": True,
                        "_plan_event": "answer",
                    }
                    if effective_policy:
                        metadata["automation_policy"] = effective_policy
                    if self._ui_instructions:
                        metadata["_ui_system_instructions"] = self._ui_instructions
                    await self._handle_message(
                        sender_id=user_id,
                        chat_id=session_id,
                        content="__plan_answer__",
                        media=[],
                        metadata={**metadata, "_diagnostic_request_id": data["request_id"]},
                        session_key=f"ui:{session_id}",
                    )
                elif msg_type == "plan_decision":
                    session_id = data.get("session_id", session_id)
                    user_id = data.get("user_id", session_id or "anonymous")
                    raw_decision = data.get("decision")
                    decision = raw_decision.strip().lower() if isinstance(raw_decision, str) else ""
                    if decision not in {"approve", "revise"}:
                        await ws.send_json(
                            {"type": "error", "content": "decision must be 'approve' or 'revise'"}
                        )
                        continue
                    raw_feedback = data.get("feedback")
                    feedback = raw_feedback.strip() if isinstance(raw_feedback, str) else ""
                    if session_id is None:
                        await ws.send_json(
                            {"type": "error", "content": "session_id required"}
                        )
                        continue
                    project_dir_path = self._resolve_project_dir(session_id, create=True)
                    if project_dir_path is None:
                        await ws.send_json(
                            {"type": "error", "content": "project_dir resolution failed"}
                        )
                        continue
                    self._clients[session_id] = ws
                    self._client_project_dirs[session_id] = project_dir_path
                    project_dir = str(project_dir_path)
                    run_mode = _normalize_run_mode(data.get("mode"))
                    agent_profile = _normalize_agent_profile(data.get("agent_profile"))
                    meta = self._persist_project_runtime_preferences(
                        project_dir_path,
                        run_mode=run_mode,
                        agent_profile=agent_profile,
                        contract_version=None,
                        automation_policy=_normalize_automation_policy(
                            data.get("automation_policy")
                        ),
                    )
                    effective_policy = _normalize_automation_policy(meta.get("automation_policy"))
                    if decision == "revise":
                        self._persist_plan_patch(project_dir_path, {"feedback": feedback})
                    self._audit(
                        source="ui",
                        action="ws_plan_decision_received",
                        session_id=session_id,
                        project_dir=project_dir_path,
                        details={"user_id": user_id, "decision": decision},
                    )
                    metadata = {
                        "source": "ui",
                        "loop_mode": "project",
                        "run_mode": run_mode,
                        "agent_profile": agent_profile,
                        "contract_version": _normalize_contract_version(
                            meta.get("contract_version")
                        ),
                        "project_dir": project_dir,
                        "_wants_stream": True,
                        "_plan_event": "decision",
                        "_plan_decision": decision,
                    }
                    if feedback:
                        metadata["_plan_feedback"] = feedback
                    if effective_policy:
                        metadata["automation_policy"] = effective_policy
                    if self._ui_instructions:
                        metadata["_ui_system_instructions"] = self._ui_instructions
                    await self._handle_message(
                        sender_id=user_id,
                        chat_id=session_id,
                        content="__plan_decision__",
                        media=[],
                        metadata={**metadata, "_diagnostic_request_id": data["request_id"]},
                        session_key=f"ui:{session_id}",
                    )
                elif msg_type == "bind":
                    session_id = data.get("session_id", session_id)
                    user_id = data.get("user_id", session_id or "anonymous")
                    if session_id is None:
                        await ws.send_json(
                            {"type": "error", "content": "session_id required"}
                        )
                        continue
                    project_dir_path = self._resolve_project_dir(session_id)
                    self._clients[session_id] = ws
                    self._client_project_dirs[session_id] = project_dir_path
                    self._audit(
                        source="ui",
                        action="ws_bind_received",
                        session_id=session_id,
                        project_dir=project_dir_path,
                        details={"user_id": user_id},
                    )
        finally:
            # Runs on errors too, so a crashed loop cannot leave a stale client bound.
            emit_event("websocket.disconnected", connection_id=connection_id, code=getattr(ws, "close_code", None))
            self._sockets.discard(ws)
            self._turn_event_sockets.discard(ws)
            if session_id and self._clients.get(session_id) is ws:
                del self._clients[session_id]
                self._client_project_dirs.pop(session_id, None)
                logger.info("WebSocket client disconnected: {}", session_id)

        return ws

    # ── REST endpoints ───────────────────────────────────────────────

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "service": "mira-gateway",
            "channel": self.name,
            "running": self._running,
            "connected_clients": len(self._clients),
        })

    async def _handle_version(self, _request: web.Request) -> web.Response:
        # Return the identity snapshot captured at boot — see __init__. Using
        # a live ``_current_engine_identity()`` here would let an in-place
        # binary swap masquerade as "already matching" and the desktop UI
        # would skip the reinstall.
        #
        # ``engine_sha256_at_boot`` is the authoritative identity field for
        # the desktop UI's fast path: it is *only* exposed by engines that
        # cache the manifest at startup, so its presence proves to the UI
        # that ``engine_sha256`` is a real boot snapshot rather than a stale
        # disk re-read. Older engines that pre-date this change still set
        # ``engine_sha256`` but lack ``engine_sha256_at_boot``, and the UI
        # treats those as untrusted and forces a one-time reinstall.
        identity = self._engine_identity
        return web.json_response({
            "service": "mira-gateway",
            "agent_version": __version__,
            "diagnostics_contract": 1,
            "api_contract": _API_CONTRACT_VERSION,
            "uptime_seconds": int(max(time.monotonic() - self._boot_ts, 0)),
            "engine_sha256": identity.get("engine_sha256"),
            "engine_sha256_at_boot": identity.get("engine_sha256"),
            "engine_manifest": identity.get("engine_manifest"),
            "engine_executable": identity.get("engine_executable"),
            "feedback_config_sha256": _feedback_sidecar_sha256(),
        })

    async def _handle_status(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "channel": self.name,
            "running": self._running,
            "connected_clients": len(self._clients),
            "uptime_host": f"{self.bind_host}:{self.bind_port}",
            "projects_root": str(self.projects_root),
            "policy": host_policy_payload(),
            "sandbox": sandbox_status_payload(),
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
        project_dir = self._resolve_project_dir(session_id)
        if project_dir is None:
            return []
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

    def _load_history_entries(
        self, session_id: str, *, workflow_run_id: str | None = None
    ) -> list[dict[str, Any]]:
        project_dir = self._resolve_project_dir(session_id)
        if project_dir is not None and project_dir.is_dir():
            manager = SessionManager(project_dir)
        elif self.workspace is not None:
            # Quick-chat (no project): read from the workspace-level session log.
            manager = SessionManager(self.workspace)
        else:
            return []
        session_key = (
            f"ui:{session_id}:run:{workflow_run_id}"
            if workflow_run_id
            else f"ui:{session_id}"
        )
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

    @staticmethod
    def _skill_history_event(details: object) -> dict[str, Any] | None:
        if not isinstance(details, dict) or details.get("loaded") is not True:
            return None
        if not all(isinstance(details.get(key), str) and details[key] for key in ("event_id", "timestamp", "skill_name", "source")):
            return None
        return {
            "id": details["event_id"], "loaded_at": details["timestamp"],
            "skill_name": details["skill_name"], "source": details["source"],
            "partial": bool(details.get("partial")),
            "run_id": details.get("workflow_run_id"), "module_type": details.get("module_type"),
            "turn_id": details.get("turn_id"), "agent": details.get("agent"),
        }

    async def _send_turn_event(self, msg: OutboundMessage) -> None:
        """Track a turn's start/finish and tell a client that asked for it; never persisted."""
        metadata = msg.metadata or {}
        event = metadata.get("_turn_event")
        turn_id = metadata.get("turn_id")
        run_id = metadata.get("workflow_run_id") if isinstance(metadata.get("workflow_run_id"), str) else ""
        if not msg.chat_id or event not in {"started", "finished"} or not isinstance(turn_id, str):
            return
        turns = self._active_turns.setdefault(msg.chat_id, {})
        if event == "started":
            turns[turn_id] = {
                "turn_id": turn_id,
                "workflow_run_id": run_id or None,
                "started_at": int(time.time()),
            }
        else:
            turns.pop(turn_id, None)
            if not turns:
                self._active_turns.pop(msg.chat_id, None)
        payload = {
            "turn_id": turn_id,
            **({"workflow_run_id": run_id} if run_id else {}),
            **({"stop_reason": metadata["stop_reason"]} if metadata.get("stop_reason") else {}),
        }
        ws = self._clients.get(msg.chat_id)
        if ws is not None and not ws.closed and ws in self._turn_event_sockets:
            try:
                await ws.send_json({
                    "type": f"turn_{event}",
                    "session_id": msg.chat_id,
                    "content": "",
                    "metadata": payload,
                })
            except Exception as exc:  # noqa: BLE001
                logger.debug("Turn event delivery deferred to /turns: {}", exc)

    async def _handle_active_turns(self, request: web.Request) -> web.Response:
        """List the session's running turns so a reconnecting client can resync."""
        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        turns = list(self._active_turns.get(session_id, {}).values())
        return web.json_response({"session_id": session_id, "active": turns})

    async def _handle_token_usage(self, request: web.Request) -> web.Response:
        from mira_engine.agent.token_usage import TokenUsageStore

        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        directory = self._resolve_project_dir(session_id) or self.workspace
        if directory is None:
            return web.json_response({"error": "workspace unavailable"}, status=503)
        try:
            snapshot = await asyncio.to_thread(TokenUsageStore(directory).snapshot, session_id)
        except Exception:
            return web.json_response({"error": "usage unavailable"}, status=503)
        return web.json_response(snapshot)

    async def _handle_skill_history(self, request: web.Request) -> web.Response:
        """Page backwards through persisted load facts, never infer success from old audits."""
        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        try:
            cursor = int(request.query["cursor"]) if "cursor" in request.query else None
            limit = int(request.query.get("limit", "100"))
            if (cursor is not None and cursor < 0) or not 1 <= limit <= 500:
                raise ValueError
        except (ValueError, TypeError):
            return web.json_response({"error": "invalid cursor or limit"}, status=400)
        project_dir = self._resolve_project_dir(session_id)
        path = project_dir / _PROJECT_AUDIT_REL_PATH if project_dir else self.projects_root / "logs" / _GLOBAL_AUDIT_FILENAME

        def read_page():
            records = deque(maxlen=limit + 1)
            if path.is_file():
                with path.open(encoding="utf-8") as stream:
                    for index, line in enumerate(stream):
                        if cursor is not None and index >= cursor:
                            break
                        try:
                            entry = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        if not isinstance(entry, dict) or entry.get("session_id") != session_id or entry.get("action") != "skill_loaded":
                            continue
                        event = self._skill_history_event(entry.get("details"))
                        if event:
                            records.append((index, event))
            more = len(records) > limit
            if more:
                records.popleft()
            return {"events": [event for _, event in records], "next_cursor": records[0][0] if more else None}

        try:
            page = await asyncio.to_thread(read_page)
        except OSError:
            return web.json_response({"error": "skill history unavailable"}, status=500)
        return web.json_response({"session_id": session_id, **page})

    async def _handle_history(self, request: web.Request) -> web.Response:
        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        raw_workflow_run_id = request.query.get("workflow_run_id")
        workflow_run_id = (
            raw_workflow_run_id.strip()
            if isinstance(raw_workflow_run_id, str) and raw_workflow_run_id.strip()
            else None
        )
        if workflow_run_id:
            try:
                run = self._workflow_api._service(session_id).repository.load_run(workflow_run_id)
            except Exception as exc:
                return web.json_response({"error": str(exc)}, status=404)
            if run.project_id != session_id:
                return web.json_response({"error": "Run belongs to another Project"}, status=409)
        response_payload = {
            "session_id": session_id,
            "entries": self._load_history_entries(
                session_id, workflow_run_id=workflow_run_id
            ),
        }
        if workflow_run_id:
            response_payload["workflow_run_id"] = workflow_run_id
        return web.json_response(response_payload)

    async def _handle_chat_context_preview(self, request: web.Request) -> web.Response:
        """Preview the same conversation text used by project promotion."""
        source_session_id = request.match_info.get("session_id", "").strip()
        if not source_session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        entries: list[dict[str, Any]] | None = None
        if request.method == "POST":
            try:
                body = await request.json()
            except Exception:
                return web.json_response({"error": "expected JSON body"}, status=400)
            raw_entries = body.get("entries") if isinstance(body, dict) else None
            if not isinstance(raw_entries, list):
                return web.json_response({"error": "entries must be a list"}, status=400)
            entries = [entry for entry in raw_entries if isinstance(entry, dict)]
        transcript, message_count = _render_chat_context_attachment(
            source_session_id,
            entries if entries is not None else self._load_history_entries(source_session_id),
        )
        if message_count == 0:
            return web.json_response({"error": "source chat has no messages"}, status=404)
        return web.json_response({
            "source_session_id": source_session_id,
            "content": transcript,
            "message_count": message_count,
        })

    async def _handle_get_config(self, _request: web.Request) -> web.Response:
        config_path = config_loader.get_config_path().expanduser().resolve()
        runtime_config = config_loader.load_config(config_path)
        payload = build_ui_runtime_payload(
            runtime_config,
            projects_root=self.projects_root,
            config_path=config_path,
            persisted=False,
        )
        payload["project_location"] = self._project_location_payload()
        payload["policy"] = host_policy_payload()
        return web.json_response(payload)

    async def _handle_config(self, request: web.Request) -> web.Response:
        """Allow the UI to inspect and update the active runtime config."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid JSON"}, status=400)

        if not isinstance(body, dict):
            return web.json_response({"error": "config payload must be an object"}, status=400)

        config_path = config_loader.get_config_path().expanduser().resolve()
        runtime_config = config_loader.load_config(config_path)
        previous_root = self.projects_root.expanduser().resolve()

        runtime_body = body.get("runtime")
        requests_root_change = "projects_root" in body or (
            isinstance(runtime_body, dict) and "workspace" in runtime_body
        )
        if requests_root_change and not self._custom_project_dirs_allowed():
            return web.json_response(
                {"error": "project root is managed by server configuration"},
                status=400,
            )

        try:
            next_root, changed = apply_ui_runtime_update(
                runtime_config,
                body,
                current_projects_root=previous_root,
            )
        except PolicyLockedError as exc:
            return web.json_response({"error": str(exc), "code": exc.code}, status=403)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        if next_root != previous_root:
            await self._close_active_clients()
            self._register_projects_under_root(previous_root)
            self.projects_root = next_root
            self.project_registry.projects_root = next_root
            self._remember_projects_root(next_root)
            self._register_projects_under_root(next_root)
            self._audit(
                source="ui",
                action="api_projects_root_updated",
                details={"projects_root": str(next_root)},
            )
            logger.info("Projects root updated to {}", next_root)

        persisted = False
        if changed:
            try:
                save_ui_runtime_update(
                    runtime_config,
                    body,
                    current_projects_root=previous_root,
                    config_path=config_path,
                )
                persisted = True
            except OSError as exc:
                logger.warning(
                    "Failed to persist runtime config {}: {}",
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

            if self._on_runtime_config_updated is not None:
                try:
                    await self._on_runtime_config_updated(runtime_config, self.projects_root)
                except Exception as exc:
                    logger.exception("Failed to apply runtime config update")
                    return web.json_response(
                        {
                            "error": f"failed to apply runtime config: {exc}",
                            "projects_root": str(self.projects_root),
                            "config_path": str(config_path),
                        },
                        status=500,
                    )

        payload = build_ui_runtime_payload(
            runtime_config,
            projects_root=self.projects_root,
            config_path=config_path,
            persisted=persisted,
        )
        payload["project_location"] = self._project_location_payload()
        return web.json_response(payload)

    async def _handle_get_profile(self, _request: web.Request) -> web.Response:
        """Return first-run profile state (for prefilling the wizard / prompts)."""
        from mira_engine.profile import get_profile_state

        config_path = config_loader.get_config_path().expanduser().resolve()
        runtime_config = config_loader.load_config(config_path)
        return web.json_response(get_profile_state(runtime_config))

    async def _handle_save_profile(self, request: web.Request) -> web.Response:
        """Persist the first-run profile: write USER.md/SOUL.md, sync config."""
        from mira_engine.profile import apply_profile

        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "profile payload must be an object"}, status=400)

        config_path = config_loader.get_config_path().expanduser().resolve()
        runtime_config = config_loader.load_config(config_path)
        try:
            result = apply_profile(runtime_config, body, config_path=config_path)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except OSError as exc:
            logger.warning("Failed to write profile files: {}", exc)
            return web.json_response({"error": f"failed to write profile: {exc}"}, status=500)

        # Hot-apply timezone/language so the running agent picks them up without a
        # restart (USER.md/SOUL.md are already re-read from the workspace each turn).
        if self._on_runtime_config_updated is not None:
            try:
                await self._on_runtime_config_updated(runtime_config, self.projects_root)
            except Exception:
                logger.exception("Failed to hot-apply config after profile save")

        self._audit(source="ui", action="profile_onboarded", details={})
        return web.json_response(result)

    # --- Mira Community Platform (#114) ----------------------------------
    @staticmethod
    def _community_status_payload(cfg: Config) -> dict[str, Any]:
        c = cfg.community
        return {
            "enabled": bool(c.enabled),
            "logged_in": bool(c.agent_token),
            "agent_id": c.agent_id or "",
            # Community name (``Name #<id8>`` on the website, ``@Name`` in mentions).
            "agent_name": c.agent_name or "",
            # The name the user gave the agent, as the join form's default.
            "suggested_name": suggested_agent_name(cfg.workspace_path) or "",
            "api_base": c.api_base,
            "autonomy_mode": c.autonomy_mode,
            "code_host": c.code_host,
            "domains": list(c.domains),
            # False until the user set up USER.local.md (desktop wizard or
            # `mira profile`); the UI offers it right after joining.
            "profile_onboarded": bool(cfg.profile.onboarded),
            # Kinds of community events the user receives (empty = none).
            "notifications": list(c.notifications or []),
        }

    async def _handle_community_status(self, _request: web.Request) -> web.Response:
        cfg = config_loader.load_config(config_loader.get_config_path().expanduser().resolve())
        payload = self._community_status_payload(cfg)
        payload["pending_count"] = len(community_approvals.list_approvals(status="pending"))
        me = await self._fetch_agent_me(cfg)
        status = me.get("status") if me else None
        payload["member_status"] = str(status) if status else None
        if me and isinstance(me.get("name"), str) and me["name"]:
            payload["agent_name"] = me["name"]
        payload["web_url"] = self._community_web_url(cfg, me)
        payload["unread"] = community_inbox.unread_counts()
        return web.json_response(payload)

    @staticmethod
    def _community_web_url(cfg: Config, me: dict[str, Any] | None) -> str:
        """The community website: config override, else what the platform
        advertises, else the production site for the production API."""
        c = cfg.community
        if c.web_url:
            return c.web_url.rstrip("/")
        if me and isinstance(me.get("web_url"), str) and me["web_url"]:
            return me["web_url"].rstrip("/")
        if (c.api_base or "").rstrip("/") == "https://mira-intelligence.com/community":
            return "https://community.mira-intelligence.com"
        return ""

    @staticmethod
    async def _fetch_agent_me(cfg: Config) -> dict[str, Any] | None:
        """Best-effort ``GET /agents/me``: lifecycle status (pending/active/
        suspended, for the Onboard action) and the advertised website. None
        when not logged in or the service is unreachable."""
        c = cfg.community
        base = (c.api_base or "").rstrip("/")
        if not (base and c.agent_token):
            return None
        try:
            async with ClientSession(timeout=ClientTimeout(total=8)) as session:
                async with session.get(
                    f"{base}/agents/me",
                    headers={"Authorization": f"Bearer {c.agent_token}"},
                ) as resp:
                    if resp.status >= 400:
                        return None
                    data = await resp.json()
        except (ClientError, asyncio.TimeoutError, json.JSONDecodeError):
            return None
        agent = data.get("agent", data) if isinstance(data, dict) else {}
        return agent if isinstance(agent, dict) else None

    async def _handle_community_inbox(self, request: web.Request) -> web.Response:
        """The user's notifications, newest first (``?kind=``, ``?unread=1``)."""
        kind = request.query.get("kind") or None
        if kind is not None and kind not in community_inbox.NOTIFICATION_KINDS:
            return web.json_response({"error": "invalid kind"}, status=400)
        unread_only = request.query.get("unread", "").lower() in ("1", "true", "yes")
        return web.json_response(
            {
                "items": community_inbox.list_items(kind=kind, unread_only=unread_only),
                "unread": community_inbox.unread_counts(),
            }
        )

    async def _handle_community_inbox_read(self, request: web.Request) -> web.Response:
        """Mark notifications read: ``{"ids": [...]}``, ``{"kind": ...}`` or ``{"all": true}``."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be an object"}, status=400)
        ids = body.get("ids")
        kind = body.get("kind")
        everything = body.get("all") is True
        if ids is not None and not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
            return web.json_response({"error": "ids must be a list of strings"}, status=400)
        if kind is not None and kind not in community_inbox.NOTIFICATION_KINDS:
            return web.json_response({"error": "invalid kind"}, status=400)
        if not (ids or kind or everything):
            return web.json_response({"error": "give ids, kind or all"}, status=400)
        changed = community_inbox.mark_read(ids, kind=kind, everything=everything)
        return web.json_response({"changed": changed, "unread": community_inbox.unread_counts()})

    async def _handle_community_notifications(self, request: web.Request) -> web.Response:
        """Choose which kinds reach the inbox: ``{"kinds": [...]}`` ([] = none)."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        try:
            kinds = community_inbox.normalize_kinds((body or {}).get("kinds") if isinstance(body, dict) else None)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        config_path = config_loader.get_config_path().expanduser().resolve()
        cfg = config_loader.load_config(config_path)
        cfg.community.notifications = kinds
        try:
            config_loader.save_config(cfg, config_path)
        except OSError as exc:
            return web.json_response({"error": f"failed to persist config: {exc}"}, status=500)
        self._audit(source="ui", action="community_notifications_updated", details={"kinds": kinds})
        return web.json_response({"notifications": kinds})

    @staticmethod
    def _community_profile_payload(cfg: Config, me: dict[str, Any]) -> dict[str, Any]:
        profile = me.get("profile") if isinstance(me.get("profile"), dict) else {}
        return {
            "agent_id": str(me.get("id") or cfg.community.agent_id or ""),
            "name": str(me.get("name") or cfg.community.agent_name or ""),
            "profile": profile,
        }

    async def _handle_community_renew_claim(self, request: web.Request) -> web.Response:
        """The user renews their agent's claim on a development proposal (the
        renew button on an expiry reminder). Their click is the approval."""
        from mira_engine.community.client import CommunityClient, CommunityError, CommunityRuleError

        cfg = config_loader.load_config(config_loader.get_config_path().expanduser().resolve())
        if not cfg.community.agent_token:
            return web.json_response({"error": "not logged in to the community"}, status=409)
        client = CommunityClient(cfg.community.api_base, cfg.community.agent_token)
        try:
            res = await client.claim_proposal(request.match_info.get("proposal_id", ""))
        except CommunityRuleError as exc:
            return web.json_response({"error": exc.reason or str(exc)}, status=409)
        except CommunityError as exc:
            return web.json_response({"error": str(exc)}, status=502)
        self._audit(source="ui", action="community_claim_renewed", details={"proposal": request.match_info.get("proposal_id")})
        claim = res.get("claim") or {}
        return web.json_response({"ok": True, "claim_expires_at": claim.get("claim_expires_at")})

    async def _handle_community_profile(self, _request: web.Request) -> web.Response:
        """The agent's community name and public profile, as the platform has them."""
        cfg = config_loader.load_config(config_loader.get_config_path().expanduser().resolve())
        if not cfg.community.agent_token:
            return web.json_response({"error": "not logged in to the community"}, status=409)
        me = await self._fetch_agent_me(cfg)
        if me is None:
            return web.json_response(
                {"error": "could not load the profile from the community service"}, status=502
            )
        return web.json_response(self._community_profile_payload(cfg, me))

    async def _handle_community_profile_update(self, request: web.Request) -> web.Response:
        """Change the community name and public profile: only the fields sent
        change. Saved on the platform, then cached locally."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        try:
            fields = normalize_profile_patch(body)
        except PublicProfileError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if not fields:
            return web.json_response({"error": "nothing to change"}, status=400)
        config_path = config_loader.get_config_path().expanduser().resolve()
        cfg = config_loader.load_config(config_path)
        c = cfg.community
        if not c.agent_token:
            return web.json_response({"error": "not logged in to the community"}, status=409)
        try:
            async with ClientSession(timeout=ClientTimeout(total=15)) as session:
                async with session.patch(
                    f"{(c.api_base or '').rstrip('/')}/agents/me/profile",
                    json=fields,
                    headers={"Authorization": f"Bearer {c.agent_token}"},
                ) as resp:
                    try:
                        data = await resp.json(content_type=None)
                    except (json.JSONDecodeError, ValueError):
                        data = None
                    if resp.status >= 400:
                        detail = data.get("error") if isinstance(data, dict) else None
                        return web.json_response(
                            {"error": f"the community rejected the change: {detail or f'HTTP {resp.status}'}"},
                            status=400 if resp.status == 400 else 502,
                        )
        except (ClientError, asyncio.TimeoutError) as exc:
            return web.json_response({"error": f"network error: {exc}"}, status=502)
        data = data if isinstance(data, dict) else {}
        profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
        # An older platform ignores ``name``: keep the cached name unless the
        # platform confirms the new one.
        if isinstance(data.get("name"), str) and data["name"]:
            c.agent_name = data["name"]
        if isinstance(profile.get("domains"), list):
            c.domains = [str(d) for d in profile["domains"]]
        try:
            config_loader.save_config(cfg, config_path)
        except OSError as exc:
            return web.json_response({"error": f"failed to persist config: {exc}"}, status=500)
        self._audit(source="ui", action="community_profile_updated", details={"fields": sorted(fields)})
        return web.json_response(
            {"agent_id": c.agent_id or "", "name": c.agent_name or "", "profile": profile}
        )

    async def _handle_community_onboard(self, _request: web.Request) -> web.Response:
        """Nudge the running agent to compose and post its own welcome reply,
        which completes onboarding. Idempotent: skips when already onboarded /
        already replied, and de-bounces rapid re-triggers while a reply is in
        flight, so the agent is never driven into a duplicate post."""
        config_path = config_loader.get_config_path().expanduser().resolve()
        cfg = config_loader.load_config(config_path)
        if not cfg.community.agent_token:
            return web.json_response(
                {"error": "not logged in — pair the agent first"}, status=400
            )

        from mira_engine.community.onboarding import build_onboarding_prompt, check_onboarding_needed

        check = await check_onboarding_needed(cfg.community)
        if check.get("error"):
            return web.json_response({"ok": False, "error": check["error"]}, status=400)
        if not check.get("needed"):
            return web.json_response(
                {"ok": True, "triggered": False, "already": True, "status": check.get("status")}
            )

        # De-bounce: a triggered reply takes a few seconds; don't drive a second
        # one until it has had a chance to land (or status flips to active).
        now = time.monotonic()
        last = getattr(self, "_onboard_triggered_at", 0.0)
        if now - last < 90.0:
            return web.json_response(
                {"ok": True, "triggered": False, "pending": True, "status": check.get("status")}
            )
        self._onboard_triggered_at = now

        msg = InboundMessage(
            channel="community",
            sender_id="community",
            chat_id=str(check["thread_id"]),
            content=await build_onboarding_prompt(cfg.community, cfg.workspace_path),
            metadata={"onboarding": True},
        )
        await self.bus.publish_inbound(msg)
        self._audit(source="ui", action="community_onboard", details={"triggered": True})
        return web.json_response({"ok": True, "triggered": True, "status": check.get("status")})

    async def _handle_community_autonomy(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        mode = body.get("mode") if isinstance(body, dict) else None
        if mode not in ("fully_autonomous", "hitl", "hybrid"):
            return web.json_response(
                {"error": "mode must be fully_autonomous, hitl, or hybrid"}, status=400
            )
        config_path = config_loader.get_config_path().expanduser().resolve()
        cfg = config_loader.load_config(config_path)
        cfg.community.autonomy_mode = mode
        try:
            config_loader.save_config(cfg, config_path)
        except OSError as exc:
            return web.json_response({"error": f"failed to persist config: {exc}"}, status=500)
        self._audit(source="ui", action="community_autonomy_updated", details={"mode": mode})
        payload = self._community_status_payload(cfg)
        payload["persisted"] = True
        return web.json_response(payload)

    async def _handle_community_approvals(self, request: web.Request) -> web.Response:
        status = request.query.get("status")
        if status is not None and status not in community_approvals.STATUSES:
            return web.json_response({"error": "invalid status filter"}, status=400)
        approvals = community_approvals.list_approvals(status=status)
        return web.json_response({"approvals": approvals})

    async def _handle_community_decide(self, request: web.Request) -> web.Response:
        approval_id = request.match_info.get("approval_id", "")
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        decision = body.get("decision") if isinstance(body, dict) else None
        if decision not in ("approve", "reject"):
            return web.json_response({"error": "decision must be approve or reject"}, status=400)

        record = community_approvals.get_approval(approval_id)
        if record is None:
            return web.json_response({"error": "approval not found"}, status=404)
        if record.get("status") != "pending":
            return web.json_response(
                {"error": f"already {record.get('status')}", "approval": record}, status=409
            )

        if decision == "reject":
            updated = community_approvals.set_status(approval_id, "rejected")
            self._audit(
                source="ui", action="community_approval_rejected", details={"id": approval_id}
            )
            return web.json_response({"ok": True, "approval": updated})

        # decision == "approve": execute against the cloud, then record the outcome.
        cfg = config_loader.load_config(config_loader.get_config_path().expanduser().resolve())
        result = await community_approvals.execute_approval(record, cfg.community)
        if not result.get("ok"):
            # Recorded as failed (not left pending) and handed back to the
            # agent, which can fix the cause and ask again (#72).
            updated = community_approvals.set_status(approval_id, "failed", detail=str(result.get("detail") or ""))
            self._audit(
                source="ui",
                action="community_approval_failed",
                details={"id": approval_id, "action": record.get("action"), "detail": result.get("detail")},
            )
            await self._follow_up_approval(record, result, cfg)
            return web.json_response(
                {"ok": False, "error": result.get("detail"), "approval": updated or record}, status=502
            )
        updated = community_approvals.set_status(approval_id, "approved")
        self._audit(
            source="ui",
            action="community_approval_approved",
            details={"id": approval_id, "action": record.get("action")},
        )
        await self._follow_up_approval(record, result, cfg)
        return web.json_response({"ok": True, "approval": updated, "result": result.get("detail")})

    async def _follow_up_approval(
        self, record: dict[str, Any], result: dict[str, Any], cfg: Config
    ) -> None:
        """Back in the conversation that asked for it, the agent picks up after a
        decision: a posted proposal is offered for building, a claimed one is
        built, an opened PR is reported, a failed execution is fixed."""
        origin = record.get("origin") or {}
        if not origin.get("channel") or not origin.get("chat_id"):
            return
        content = await community_approvals.follow_up_prompt(record, result, cfg.community)
        if not content:
            return
        await self.bus.publish_inbound(
            InboundMessage(
                channel="system",
                sender_id="community",
                chat_id=f"{origin['channel']}:{origin['chat_id']}",
                content=content,
                metadata={"project_id": origin["project_id"]} if origin.get("project_id") else {},
            )
        )

    async def _handle_community_pair_start(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            body = {}
        if not isinstance(body, dict):
            body = {}

        config_path = config_loader.get_config_path().expanduser().resolve()
        cfg = config_loader.load_config(config_path)
        api_base = (body.get("api_base") or cfg.community.api_base or "").rstrip("/")
        if not api_base:
            return web.json_response({"error": "api_base is not configured"}, status=400)

        autonomy = body.get("autonomy_mode")
        if autonomy in ("fully_autonomous", "hitl", "hybrid"):
            cfg.community.autonomy_mode = autonomy
        code_host = body.get("code_host")
        if code_host in ("github", "cnb"):
            cfg.community.code_host = code_host
        raw_profile = body.get("profile") if isinstance(body.get("profile"), dict) else {}
        if isinstance(body.get("domains"), list) and "domains" not in raw_profile:
            raw_profile = {**raw_profile, "domains": body["domains"]}
        if not raw_profile.get("name"):
            # The name the user gave the agent, unless the form named it.
            suggested = suggested_agent_name(cfg.workspace_path)
            if suggested:
                raw_profile = {**raw_profile, "name": suggested}
        try:
            public_profile = normalize_public_profile(raw_profile)
        except PublicProfileError as exc:
            return web.json_response({"error": f"public profile: {exc}"}, status=400)
        if "domains" in public_profile or "domains" in raw_profile:
            cfg.community.domains = public_profile.get("domains", [])
        cfg.community.api_base = api_base
        try:
            config_loader.save_config(cfg, config_path)
        except OSError:
            pass

        start_payload = pairing_payload(
            {
                "code_host": cfg.community.code_host,
                "autonomy_mode": cfg.community.autonomy_mode,
                "domains": cfg.community.domains,
            },
            public_profile,
        )
        try:
            async with ClientSession(timeout=ClientTimeout(total=30)) as session:
                async with session.post(
                    f"{api_base}/agents/pair/start", json=start_payload
                ) as resp:
                    if resp.status == 404:
                        return web.json_response(
                            {"error": "pairing endpoint unavailable on the community service"},
                            status=502,
                        )
                    if resp.status >= 400:
                        # Surface the platform's reason (e.g. an invalid profile field).
                        try:
                            detail = (await resp.json()).get("error")
                        except (ClientError, json.JSONDecodeError, AttributeError, ValueError):
                            detail = None
                        return web.json_response(
                            {"error": f"pairing failed: {detail or f'HTTP {resp.status}'}"},
                            status=400 if resp.status == 400 else 502,
                        )
                    data = await resp.json()
        except (ClientError, asyncio.TimeoutError) as exc:
            return web.json_response({"error": f"network error: {exc}"}, status=502)
        return web.json_response(data)

    async def _handle_community_pair_poll(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        poll_token = body.get("poll_token") if isinstance(body, dict) else None
        if not poll_token:
            return web.json_response({"error": "poll_token required"}, status=400)

        config_path = config_loader.get_config_path().expanduser().resolve()
        cfg = config_loader.load_config(config_path)
        api_base = (body.get("api_base") or cfg.community.api_base or "").rstrip("/")
        try:
            async with ClientSession(timeout=ClientTimeout(total=30)) as session:
                async with session.post(
                    f"{api_base}/agents/pair/poll", json={"poll_token": poll_token}
                ) as resp:
                    if resp.status in (202, 425):
                        return web.json_response({"status": "pending"})
                    if resp.status == 410:
                        return web.json_response({"status": "expired"})
                    if resp.status >= 400:
                        return web.json_response(
                            {"error": f"poll failed: HTTP {resp.status}"}, status=502
                        )
                    data = await resp.json()
        except (ClientError, asyncio.TimeoutError) as exc:
            return web.json_response({"error": f"network error: {exc}"}, status=502)

        agent_token = data.get("agent_token")
        if not agent_token:
            return web.json_response({"status": "pending"})

        cfg.community.enabled = True
        cfg.community.api_base = api_base
        cfg.community.agent_token = agent_token
        cfg.community.agent_id = data.get("agent_id", "")
        # The platform has the final say on the name (it falls back to a default).
        me = await self._fetch_agent_me(cfg)
        cfg.community.agent_name = str((me or {}).get("name") or "")
        try:
            config_loader.save_config(cfg, config_path)
        except OSError as exc:
            return web.json_response({"error": f"failed to persist config: {exc}"}, status=500)
        self._audit(
            source="ui",
            action="community_paired",
            details={"agent_id": cfg.community.agent_id},
        )
        payload = self._community_status_payload(cfg)
        payload["status"] = "authorized"
        return web.json_response(payload)

    async def _handle_team_layer(self, request: web.Request) -> web.Response:
        """Mira Server pushes the team layer (config/team_layer.py) here."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(body, dict) or "layer" not in body:
            return web.json_response({"error": "body must be {\"layer\": object or null}"}, status=400)
        try:
            layer = team_layer.parse_layer(body["layer"])
        except team_layer.TeamLayerError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if team_layer.set_team_layer(layer):
            self._audit(source="ui", action="api_team_layer_updated", details=layer.summary())
            logger.info("Team layer updated (revision {})", layer.revision or "-")
            if self._on_runtime_config_updated is not None:
                config_path = config_loader.get_config_path().expanduser().resolve()
                runtime_config = config_loader.load_config(config_path)
                try:
                    await self._on_runtime_config_updated(runtime_config, self.projects_root)
                except Exception as exc:
                    logger.exception("Failed to apply the team layer")
                    return web.json_response({"error": f"failed to apply the team layer: {exc}"}, status=500)
        return web.json_response({"ok": True, "revision": layer.revision})

    @staticmethod
    def _provider_models_response(cache: ModelCache) -> dict[str, Any]:
        return {
            "provider": cache.provider,
            "api_base": cache.api_base,
            "fetched_at": cache.fetched_at,
            "models": [model.config_model for model in cache.models],
            "model_details": [
                {
                    "id": model.id,
                    "config_model": model.config_model,
                    "display_name": model.display_name,
                    "context_window_tokens": model.context_window_tokens,
                }
                for model in cache.models
            ],
        }

    async def _handle_provider_models(self, request: web.Request) -> web.Response:
        """Fetch (or read cached) available models for a provider."""
        raw_provider_name = request.match_info.get("name", "").strip()
        if not is_custom_provider_id(raw_provider_name) and find_by_name(raw_provider_name or "_") is None:
            return web.json_response({"error": f"unknown provider: {raw_provider_name}"}, status=400)
        config_path = config_loader.get_config_path().expanduser().resolve()
        runtime_config = config_loader.load_config(config_path)
        provider_name = _resolve_ui_provider_name(runtime_config, raw_provider_name)
        if provider_name is None:
            return web.json_response({"error": f"unknown provider: {raw_provider_name}"}, status=400)

        refresh = request.query.get("refresh", "").lower() in ("1", "true", "yes")

        if not refresh:
            cache = read_model_cache(provider_name, config_path)
            if cache is not None and not is_cache_stale(cache):
                payload = self._provider_models_response(cache)
                payload["cached"] = True
                return web.json_response(payload)

        try:
            cache, _ = await fetch_models_to_cache(runtime_config, provider_name, config_path)
        except ModelFetchError as exc:
            return web.json_response({"error": str(exc)}, status=502)
        except Exception as exc:  # noqa: BLE001 - surface as actionable error
            logger.warning("Provider model fetch failed for {}: {}", provider_name, exc)
            return web.json_response({"error": f"model fetch failed: {exc}"}, status=502)

        payload = self._provider_models_response(cache)
        payload["cached"] = False
        return web.json_response(payload)

    async def _handle_provider_test(self, request: web.Request) -> web.Response:
        """Validate provider connectivity using optional transient credentials."""
        raw_provider_name = request.match_info.get("name", "").strip()

        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            body = {}
        if body is None:
            body = {}
        if not isinstance(body, dict):
            return web.json_response({"error": "request body must be an object"}, status=400)

        config_path = config_loader.get_config_path().expanduser().resolve()
        runtime_config = config_loader.load_config(config_path)
        provider_name = _resolve_ui_provider_name(runtime_config, raw_provider_name)
        provider_cfg = runtime_config.providers.entry(provider_name) if provider_name else None
        if provider_name is None or provider_cfg is None:
            return web.json_response({"error": f"unknown provider: {raw_provider_name}"}, status=400)

        # Apply transient overrides to the in-memory config only (never persisted).
        api_key = body.get("api_key")
        if isinstance(api_key, str) and api_key.strip():
            provider_cfg.api_key = api_key.strip()
        if "api_base" in body:
            api_base = body["api_base"]
            provider_cfg.api_base = None if api_base in (None, "") else str(api_base).strip()

        try:
            cache = await fetch_provider_models(runtime_config, provider_name)
        except ModelFetchError as exc:
            return web.json_response({"ok": False, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - report failure to the UI
            return web.json_response({"ok": False, "message": f"connection failed: {exc}"})

        return web.json_response(
            {
                "ok": True,
                "message": "Connection succeeded",
                "model_count": len(cache.models),
            }
        )

    async def _handle_feedback_config(self, _request: web.Request) -> web.Response:
        relay = _resolve_feedback_config(self.config)
        return web.json_response({
            "configured": relay.configured,
            "invite_url": relay.feishu_invite_url or None,
        })

    async def _submit_feishu_feedback(
        self,
        relay: _FeedbackRelayConfig,
        payload: dict[str, Any],
    ) -> None:
        await self._post_feishu_webhook(relay, {
            "msg_type": "text",
            "content": {
                "text": _build_feedback_agent_text(
                    payload,
                    mention_open_id=relay.feishu_mention_open_id,
                    mention_name=relay.feishu_mention_name,
                ),
            },
        })

    async def _post_feishu_webhook(
        self,
        relay: _FeedbackRelayConfig,
        body: dict[str, Any],
    ) -> None:
        timestamp_sec = str(int(time.time()))
        body = dict(body)
        if relay.feishu_secret:
            body["timestamp"] = timestamp_sec
            body["sign"] = _feishu_sign(timestamp_sec, relay.feishu_secret)

        try:
            await self._post_feishu_webhook_once(relay, body)
        except ClientError as exc:
            if "CERTIFICATE_VERIFY_FAILED" not in str(exc) and "certificate verify failed" not in str(exc):
                raise RuntimeError(f"feishu webhook request failed: {exc}") from exc
            logger.warning("SSL certificate verification failed for Feishu feedback webhook; retrying with ssl=False")
            try:
                await self._post_feishu_webhook_once(relay, body, ssl=False)
            except (ClientError, asyncio.TimeoutError) as retry_exc:
                raise RuntimeError(f"feishu webhook request failed: {retry_exc}") from retry_exc
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"feishu webhook request failed: {exc}") from exc

    async def _post_feishu_webhook_once(
        self,
        relay: _FeedbackRelayConfig,
        body: dict[str, Any],
        *,
        ssl: bool | None = None,
    ) -> None:
        timeout = ClientTimeout(total=_FEEDBACK_TIMEOUT_SECONDS)
        request_kwargs: dict[str, Any] = {
            "json": body,
            "headers": {"Content-Type": "application/json"},
        }
        if ssl is not None:
            request_kwargs["ssl"] = ssl
        async with ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(
                relay.feishu_webhook_url,
                **request_kwargs,
            ) as resp:
                text = await resp.text()
                if not 200 <= resp.status < 300:
                    raise RuntimeError(f"feishu webhook returned {resp.status}")
                if text:
                    try:
                        parsed = json.loads(text)
                    except json.JSONDecodeError:
                        parsed = None
                    if isinstance(parsed, dict) and parsed.get("code") not in {None, 0}:
                        raise RuntimeError(
                            f"feishu webhook error {parsed.get('code')}: {parsed.get('msg') or 'unknown'}"
                        )

    async def _handle_feedback(self, request: web.Request) -> web.Response:
        relay = _resolve_feedback_config(self.config)
        if not relay.configured:
            return web.json_response({"error": "not_configured"}, status=503)

        try:
            body = await request.json()
            payload = _sanitize_feedback_payload(body)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

        try:
            await self._submit_feishu_feedback(relay, payload)
        except RuntimeError as exc:
            logger.warning("Failed to submit UI feedback to Feishu: {}", exc)
            return web.json_response({"error": str(exc)}, status=502)

        return web.json_response({
            "ok": True,
            "channel": "feishu",
            "invite_url": relay.feishu_invite_url or None,
        })

    def _workspace_root_for_access(self) -> Path:
        """Return the root path used for workspace access checks."""
        return self.projects_root.expanduser().resolve()

    def _project_storage_mode(self) -> str:
        mode = getattr(self.config, "project_storage", "user_selectable")
        return "managed" if mode == "managed" else "user_selectable"

    def _custom_project_dirs_allowed(self) -> bool:
        return self._project_storage_mode() != "managed"

    def _project_location_payload(self) -> dict[str, Any]:
        return {
            "mode": self._project_storage_mode(),
            "custom_dir_allowed": self._custom_project_dirs_allowed(),
            "default_parent_dir": str(self.projects_root),
            "workspace_file": str(self._project_workspace_path),
        }

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

        if body.get("purpose") == "experiment_input":
            # A read-only user-facing probe, not an enduring grant. Authority is
            # recorded only when the user creates the experiment Run.
            try:
                resolved = await asyncio.to_thread(resolve_input_path, raw_path)
            except (OSError, ValueError, RuntimeError) as exc:
                return web.json_response({"ok": False, "error": str(exc)})
            return web.json_response({
                "ok": True,
                "kind": "directory" if resolved.is_dir() else "file",
                "resolved_path": str(resolved),
                "outside_workspace": not resolved.is_relative_to(self._workspace_root_for_access().resolve()),
                "restrict_to_workspace": self.restrict_to_workspace,
            })

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
        """Serve a legacy-compatible read model, never write Run records to a plan."""
        session_id = request.query.get("session_id")
        if not session_id:
            return web.json_response(None)

        try:
            data = self._load_plan_data(session_id)
        except ValueError as exc:
            logger.warning(str(exc))
            return web.json_response({"error": str(exc)}, status=500)
        project_dir = self._resolve_project_dir(session_id)
        if project_dir is not None:
            from mira_engine.workflows import JsonWorkflowRepository
            from mira_engine.workflows.experiments import project_experiments

            records = project_experiments(JsonWorkflowRepository(project_dir, session_id))
            if records:
                data = {**(data or {}), "experiments": [*(data or {}).get("experiments", []), *records]}
        if data is None:
            return web.json_response(None)
        return web.json_response(data)

    async def _handle_plan_contract(self, request: web.Request) -> web.Response:
        """Serve resolved task-plan contract requirements for one project."""
        session_id = (request.query.get("session_id") or "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        project_dir = self._resolve_project_dir(session_id)
        if project_dir is None or not project_dir.is_dir():
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
        project_dir = self._resolve_project_dir(session_id)
        if project_dir is None or not project_dir.is_dir():
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
        return self.project_registry.project_meta_path(project_dir)

    def _is_project_dir(self, project_dir: Path) -> bool:
        return (
            project_dir.is_dir()
            and (
                self.project_registry.is_registered_project_dir(project_dir)
                or self.project_registry.is_legacy_project_dir(project_dir)
            )
        )

    def _load_project_meta(self, project_dir: Path) -> dict[str, Any]:
        return self.project_registry.load_project_meta(project_dir)

    def _default_project_meta(self, project_dir: Path) -> dict[str, Any]:
        project_id = self.project_registry.project_id_for_dir(project_dir) or project_dir.name
        return self.project_registry.default_project_meta(project_id, project_dir)

    def _write_project_meta(self, project_dir: Path, meta: dict[str, Any]) -> None:
        self.project_registry.write_project_meta(project_dir, meta)

    def _ensure_project_meta(self, project_dir: Path) -> dict[str, Any]:
        project_dir = project_dir.expanduser().resolve()
        project_id = self.project_registry.project_id_for_dir(project_dir) or project_dir.name
        try:
            return self.project_registry.ensure_project_meta(project_id, project_dir)
        except ValueError:
            return self.project_registry.ensure_project_meta(
                slugify_project_id(project_id),
                project_dir,
                display_name=project_id,
            )

    def _persist_project_runtime_preferences(
        self,
        project_dir: Path,
        *,
        run_mode: str,
        agent_profile: str,
        contract_version: int | None,
        automation_policy: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Persist runtime preferences for websocket-driven project sessions."""
        project_id = self.project_registry.project_id_for_dir(project_dir) or project_dir.name
        project_dir = self._register_project_dir(project_id, project_dir)
        project_dir.mkdir(parents=True, exist_ok=True)
        meta = self._ensure_project_meta(project_dir)
        changed = False

        if meta.get("run_mode") != run_mode:
            meta["run_mode"] = run_mode
            changed = True
        if meta.get("agent_profile") != agent_profile:
            meta["agent_profile"] = agent_profile
            changed = True
        if contract_version is not None:
            normalized_contract = _normalize_contract_version(contract_version)
            if _normalize_contract_version(meta.get("contract_version")) != normalized_contract:
                meta["contract_version"] = normalized_contract
                changed = True

        normalized_policy = _normalize_automation_policy(automation_policy)
        if meta.get("automation_policy") != normalized_policy:
            meta["automation_policy"] = normalized_policy
            changed = True

        if changed:
            meta["updated_at"] = f"{datetime.utcnow().isoformat()}Z"
            self._write_project_meta(project_dir, meta)
        return meta

    def _project_info(self, ref: ProjectRef) -> dict[str, Any]:
        project_id = ref.project_id
        project_dir = ref.project_dir
        meta = self._ensure_project_meta(project_dir)
        info: dict[str, Any] = {
            "id": project_id,
            "display_name": str(meta.get("display_name", project_id)),
            "project_dir": str(project_dir),
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
        plan_file = project_dir / PLAN_FILENAME
        if plan_file.is_file():
            try:
                plan = json.loads(plan_file.read_text(encoding="utf-8"))
                if not isinstance(plan, dict):
                    raise ValueError(f"Unexpected non-object JSON in {plan_file}")
                if self._reconcile_plan_data(project_dir, plan):
                    try:
                        plan_file.write_text(
                            json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                    except OSError as exc:
                        logger.warning("Failed to write reconciled {}: {}", plan_file, exc)
                info["title"] = plan.get("title", "")
                info["status"] = plan.get("status", "in_progress")
                info["core_question"] = plan.get("core_question", "")
                info["started_at"] = plan.get("started_at", "")
                info["has_plan"] = True
            except (ValueError, json.JSONDecodeError, OSError):
                info["has_plan"] = False
        else:
            info["has_plan"] = False
        return info

    async def _handle_list_projects(self, _request: web.Request) -> web.Response:
        """List projects from the workspace registry plus legacy PRJ-* folders."""
        refs = self.project_registry.list_projects()
        projects = [self._project_info(ref) for ref in refs]
        return web.json_response({"projects": projects})

    async def _organization_json_body(self, request: web.Request) -> dict[str, Any] | None:
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            return None
        return body if isinstance(body, dict) else None

    async def _handle_organization(self, _request: web.Request) -> web.Response:
        return web.json_response(self.organization_store.snapshot())

    async def _handle_create_folder(self, request: web.Request) -> web.Response:
        body = await self._organization_json_body(request)
        if body is None:
            return web.json_response({"error": "invalid JSON object"}, status=400)
        try:
            folder = self.organization_store.create_folder(body.get("name"))
        except FileExistsError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except (OSError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(folder, status=201)

    async def _handle_rename_folder(self, request: web.Request) -> web.Response:
        body = await self._organization_json_body(request)
        if body is None:
            return web.json_response({"error": "invalid JSON object"}, status=400)
        folder_id = request.match_info.get("folder_id", "")
        try:
            folder = self.organization_store.rename_folder(folder_id, body.get("name"))
        except KeyError:
            return web.json_response({"error": "folder not found"}, status=404)
        except FileExistsError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except (OSError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(folder)

    async def _handle_delete_folder(self, request: web.Request) -> web.Response:
        folder_id = request.match_info.get("folder_id", "")
        try:
            self.organization_store.delete_folder(folder_id)
        except KeyError:
            return web.json_response({"error": "folder not found"}, status=404)
        except (OSError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"deleted": True, "folder_id": folder_id})

    async def _handle_assign_folder(self, request: web.Request) -> web.Response:
        body = await self._organization_json_body(request)
        if body is None:
            return web.json_response({"error": "invalid JSON object"}, status=400)
        kind = request.match_info.get("kind", "")
        item_id = request.match_info.get("item_id", "")
        snapshot = self.organization_store.snapshot()
        if kind == "chat":
            exists = any(chat.get("id") == item_id for chat in snapshot["chats"])
            if not exists:
                try:
                    self.organization_store.upsert_chats([{"id": item_id, "title": ""}])
                    exists = True
                except (OSError, ValueError):
                    exists = False
        elif kind == "project":
            exists = self._resolve_project_dir(item_id) is not None
        else:
            return web.json_response({"error": "kind must be chat or project"}, status=400)
        if not exists:
            return web.json_response({"error": f"{kind} not found"}, status=404)
        try:
            self.organization_store.assign(kind, item_id, body.get("folder_id"))
        except KeyError:
            return web.json_response({"error": "folder not found"}, status=404)
        except (OSError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"kind": kind, "item_id": item_id, "folder_id": body.get("folder_id")})

    async def _handle_import_chats(self, request: web.Request) -> web.Response:
        body = await self._organization_json_body(request)
        if body is None:
            return web.json_response({"error": "invalid JSON object"}, status=400)
        try:
            chats = self.organization_store.upsert_chats(body.get("chats"))
        except (OSError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"chats": chats})

    async def _handle_update_chat(self, request: web.Request) -> web.Response:
        body = await self._organization_json_body(request)
        if body is None:
            return web.json_response({"error": "invalid JSON object"}, status=400)
        chat_id = request.match_info.get("chat_id", "")
        try:
            chat = self.organization_store.update_chat(
                chat_id,
                title=body.get("title") if "title" in body else None,
                touch=body.get("touch") is True,
            )
        except KeyError:
            return web.json_response({"error": "chat not found"}, status=404)
        except (OSError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(chat)

    async def _handle_delete_chat(self, request: web.Request) -> web.Response:
        chat_id = request.match_info.get("chat_id", "")
        try:
            self.organization_store.delete_chat(chat_id)
        except KeyError:
            return web.json_response({"error": "chat not found"}, status=404)
        except (OSError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"deleted": True, "chat_id": chat_id})

    async def _handle_create_project(self, request: web.Request) -> web.Response:
        """Create/register a project in the workspace file."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "JSON body must be an object"}, status=400)

        raw_display_name = body.get("display_name") or body.get("name") or body.get("title")
        display_name = raw_display_name.strip() if isinstance(raw_display_name, str) else ""
        raw_project_id = body.get("project_id") or body.get("id")
        project_id = raw_project_id.strip() if isinstance(raw_project_id, str) else ""
        if not project_id:
            project_id = slugify_project_id(display_name)

        try:
            project_id = validate_project_id(project_id)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        project_dir = body.get("project_dir")
        project_parent_dir = body.get("project_parent_dir")
        if not self._custom_project_dirs_allowed():
            project_dir = None
            project_parent_dir = None
        else:
            if project_dir is not None and not isinstance(project_dir, str):
                return web.json_response({"error": "project_dir must be a string"}, status=400)
            if project_parent_dir is not None and not isinstance(project_parent_dir, str):
                return web.json_response({"error": "project_parent_dir must be a string"}, status=400)

        try:
            ref = self.project_registry.create_project(
                project_id=project_id,
                display_name=display_name or project_id,
                project_dir=project_dir,
                project_parent_dir=project_parent_dir,
                allow_existing_dir=body.get("allow_existing_dir") is True,
            )
        except ProjectDirectoryExistsError as exc:
            return web.json_response({
                "error": str(exc),
                "code": "project_directory_exists",
                "project_dir": str(exc.project_dir),
            }, status=409)
        except FileExistsError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except (OSError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

        meta = self._ensure_project_meta(ref.project_dir)
        changed = False
        run_mode = body.get("run_mode")
        if isinstance(run_mode, str):
            meta["run_mode"] = _normalize_run_mode(run_mode)
            changed = True
        agent_profile = body.get("agent_profile")
        if isinstance(agent_profile, str):
            meta["agent_profile"] = _normalize_agent_profile(agent_profile)
            changed = True
        contract_version = body.get("contract_version")
        if isinstance(contract_version, int):
            meta["contract_version"] = _normalize_contract_version(contract_version)
            changed = True
        automation_policy = body.get("automation_policy")
        if isinstance(automation_policy, (dict, type(None))):
            meta["automation_policy"] = _normalize_automation_policy(automation_policy)
            changed = True
        if changed:
            meta["updated_at"] = f"{datetime.utcnow().isoformat()}Z"
            self._write_project_meta(ref.project_dir, meta)
            self.project_registry.save()

        self._audit(
            source="ui",
            action="api_project_created",
            session_id=ref.project_id,
            project_dir=ref.project_dir,
            details={
                "display_name": meta.get("display_name"),
                "storage_mode": self._project_storage_mode(),
            },
        )

        return web.json_response(self._project_info(ref), status=201)

    async def _handle_project_meta(self, request: web.Request) -> web.Response:
        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)

        project_dir = self._resolve_project_dir(session_id)
        if project_dir is None or not self._is_project_dir(project_dir):
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

        project_dir = self._resolve_project_dir(session_id)
        if project_dir is None or not project_dir.is_dir():
            self._audit(
                source="ui",
                action="api_delete_project_missing",
                session_id=session_id,
                details={"reason": "not found"},
            )
            return web.json_response({"deleted": False, "removed": False, "reason": "not found"})

        try:
            self._audit(
                source="ui",
                action="api_delete_project_requested",
                session_id=session_id,
                project_dir=project_dir,
            )
            shutil.rmtree(project_dir)
            self._drop_project_dir_registration(session_id)
            try:
                self.organization_store.remove_project(session_id)
            except OSError as exc:
                logger.warning("Failed to clean organization mapping for {}: {}", session_id, exc)
            self._client_project_dirs.pop(session_id, None)
            self._audit(
                source="ui",
                action="api_delete_project_completed",
                session_id=session_id,
            )
            logger.info("Deleted project directory: {}", project_dir)
            return web.json_response({"deleted": True, "removed": True})
        except OSError as exc:
            self._audit(
                source="ui",
                action="api_delete_project_failed",
                session_id=session_id,
                details={"error": self._preview(str(exc), limit=400)},
            )
            logger.warning("Failed to delete {}: {}", project_dir, exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_remove_project(self, request: web.Request) -> web.Response:
        """Remove a project from the UI registry without deleting local files."""
        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        try:
            session_id = validate_project_id(session_id)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        project_dir = self._resolve_project_dir(session_id)
        self._audit(
            source="ui",
            action="api_remove_project_requested",
            session_id=session_id,
            project_dir=project_dir,
        )
        try:
            self._drop_project_dir_registration(session_id, hide=True)
            try:
                self.organization_store.remove_project(session_id)
            except OSError as exc:
                logger.warning("Failed to clean organization mapping for {}: {}", session_id, exc)
            self._client_project_dirs.pop(session_id, None)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        self._audit(
            source="ui",
            action="api_remove_project_completed",
            session_id=session_id,
            project_dir=project_dir,
            details={"files_preserved": True},
        )
        return web.json_response({"deleted": False, "removed": True})

    async def _handle_list_project_files(self, request: web.Request) -> web.Response:
        """List files under projects_root/<session_id> for the UI resource explorer."""
        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)

        project_dir = self._resolve_project_dir(session_id)
        if project_dir is None or not project_dir.is_dir():
            return web.json_response({"error": "project not found"}, status=404)

        files, truncated = await asyncio.to_thread(_collect_project_file_entries, project_dir)
        return web.json_response({
            "session_id": session_id,
            "files": files,
            "truncated": truncated,
        })

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

        project_dir = self._resolve_project_dir(session_id, create=True)
        if project_dir is None:
            return web.json_response({"error": "project not found"}, status=404)
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

            preserve_relative_path = target == "data"
            destination = _resolve_upload_destination(
                upload_dir,
                part.filename,
                preserve_relative_path=preserve_relative_path,
            )
            if destination is None:
                await part.release()
                if preserve_relative_path:
                    return web.json_response(
                        {"error": f"unsafe upload path: {part.filename}"},
                        status=400,
                    )
                continue
            if target == "references":
                suffix = destination.suffix.lower()
                if suffix not in {".pdf", ".zip"}:
                    return web.json_response(
                        {
                            "error": (
                                "references uploads only support .pdf and .zip files"
                            )
                        },
                        status=400,
                    )

            size = 0
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
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

    async def _handle_project_knowledge(self, request: web.Request) -> web.Response:
        """Read or replace the knowledge bases attached to a project."""
        session_id = request.match_info.get("session_id", "").strip()
        project_dir = self._resolve_project_dir(session_id) if session_id else None
        if project_dir is None or not self._is_project_dir(project_dir):
            return web.json_response({"error": "project not found"}, status=404)
        meta = self._ensure_project_meta(project_dir)
        if request.method == "PUT":
            try:
                body = await request.json()
            except (json.JSONDecodeError, TypeError):
                return web.json_response({"error": "invalid JSON"}, status=400)
            raw = body.get("knowledge_bases") if isinstance(body, dict) else None
            if not isinstance(raw, list):
                return web.json_response({"error": "knowledge_bases must be a list"}, status=400)
            kb_ids = clean_kb_ids(raw)
            known = {kb.id for kb in self._knowledge.store.list()}
            unknown = [kb_id for kb_id in kb_ids if kb_id not in known]
            if unknown:
                return web.json_response({"error": f"unknown knowledge bases: {', '.join(unknown)}"}, status=400)
            meta[PROJECT_META_KEY] = kb_ids
            meta["updated_at"] = f"{datetime.utcnow().isoformat()}Z"
            self._write_project_meta(project_dir, meta)
            self._audit(
                source="ui", action="project_knowledge_updated", session_id=session_id,
                project_dir=project_dir, details={"knowledge_bases": kb_ids},
            )
        return web.json_response({"session_id": session_id, "knowledge_bases": clean_kb_ids(meta.get(PROJECT_META_KEY))})

    async def _handle_attach_chat_context(self, request: web.Request) -> web.Response:
        """Save a Quick Chat transcript as a readable project context file."""
        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "expected JSON body"}, status=400)
        source_session_id = _string_value(
            body.get("source_session_id") if isinstance(body, dict) else None
        )
        if not source_session_id:
            return web.json_response({"error": "source_session_id required"}, status=400)
        if source_session_id == session_id:
            return web.json_response(
                {"error": "source session must differ from project session"}, status=400
            )

        project_dir = self._resolve_project_dir(session_id)
        if project_dir is None or not project_dir.is_dir():
            return web.json_response({"error": "project not found"}, status=404)

        raw_entries = body.get("entries") if isinstance(body, dict) else None
        if raw_entries is not None and not isinstance(raw_entries, list):
            return web.json_response({"error": "entries must be a list"}, status=400)
        entries = (
            [entry for entry in raw_entries if isinstance(entry, dict)]
            if isinstance(raw_entries, list)
            else self._load_history_entries(source_session_id)
        )
        transcript, message_count = _render_chat_context_attachment(
            source_session_id,
            entries,
        )
        if message_count == 0:
            return web.json_response({"error": "source chat has no messages"}, status=404)

        source_slug = slugify_project_id(source_session_id)[:48]
        source_hash = hashlib.sha256(source_session_id.encode("utf-8")).hexdigest()[:8]
        context_dir = project_dir / "context"
        destination = context_dir / f"quick-chat-{source_slug}-{source_hash}.md"
        try:
            context_dir.mkdir(parents=True, exist_ok=True)
            destination.write_text(transcript, encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to save chat context {}: {}", destination, exc)
            return web.json_response({"error": str(exc)}, status=500)

        relative_path = destination.relative_to(project_dir).as_posix()
        self._audit(
            source="ui",
            action="api_project_chat_context_attached",
            session_id=session_id,
            project_dir=project_dir,
            details={
                "source_session_id": source_session_id,
                "path": relative_path,
                "message_count": message_count,
            },
        )
        return web.json_response({
            "session_id": session_id,
            "source_session_id": source_session_id,
            "path": relative_path,
            "message_count": message_count,
        }, status=201)

    async def _handle_project_artifact(self, request: web.Request) -> web.Response:
        """Serve a project file under projects_root/<session_id> by relative path."""
        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)

        rel_path = request.query.get("path", "").strip()
        if not rel_path:
            return web.json_response({"error": "path required"}, status=400)

        project_dir = self._resolve_project_dir(session_id)
        if project_dir is None or not project_dir.is_dir():
            return web.json_response({"error": "project not found"}, status=404)

        candidate = (project_dir / rel_path).resolve()
        try:
            candidate.relative_to(project_dir)
        except ValueError:
            return web.json_response({"error": "invalid artifact path"}, status=400)

        if not candidate.is_file():
            return web.json_response({"error": "artifact not found"}, status=404)

        if request.query.get("download") == "1":
            # The UI and engine are usually different origins, where the
            # browser ignores <a download>, so the header has to ask for it.
            return web.FileResponse(
                candidate,
                headers={"Content-Disposition": _attachment_disposition(candidate.name)},
            )
        if is_deck_file(candidate):
            return deck_response(candidate, edit=request.query.get("mode") == "edit")
        return web.FileResponse(candidate)

    def _skill_plugin_manager(self, session_id: str) -> SkillPluginManager:
        # Skills are managed at a single global scope. The UI sends a sentinel
        # session id so plugins can be listed/installed without first selecting
        # a project; route it to the global workspace (plugins + global state
        # already live there regardless of the manager's workspace argument).
        if session_id == _GLOBAL_SKILLS_SESSION_ID:
            return SkillPluginManager(get_workspace_path(None))
        project_dir = self._resolve_project_dir(session_id, create=True)
        if project_dir is None:
            raise SkillPluginError(f"project not found: {session_id}")
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
        if refusal := plugin_install_refusal():
            return refusal
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
        if refusal := plugin_install_refusal():
            return refusal

        manager = self._skill_plugin_manager(session_id)
        try:
            manager.uninstall(plugin_id)
        except SkillPluginError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        return web.json_response({"uninstalled": plugin_id, "plugins": manager.list_plugins()})
