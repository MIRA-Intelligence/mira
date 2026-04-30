"""Research-flavoured agent loop.

:class:`ResearchAgentLoop` extends :class:`BaseAgentLoop` with the
Mira-specific orchestration that powers the desktop UI: agent profiles,
auto-mode while-loops, task-plan guardrails, automation stop policies, and
cumulative session token tracking. The split lets ``mira agent`` stay
nanobot-shaped while ``mira research`` (and the ``gateway`` web channel)
keep their richer behaviour.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from mira_engine.agent.base_loop import BaseAgentLoop
from mira_engine.agent.context import ContextBuilder
from mira_engine.agent.tools.message import MessageTool
from mira_engine.bus.events import InboundMessage, OutboundMessage
from mira_engine.command.router import CommandContext
from mira_engine.task_plan.guardrails import (
    get_task_plan_contract,
    guard_task_plan_file,
    plan_has_final_result_output,
)


class ResearchAgentLoop(BaseAgentLoop):
    """Research-flavoured agent loop with auto-mode and task-plan contracts."""

    _AUTO_MAX_ROUNDS = 20
    _AUTO_GUARD_REPAIR_MAX = 1
    _AUTO_CHECKPOINT_REPAIR_MAX = 1

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._session_run_modes: dict[str, str] = {}
        self._session_agent_profiles: dict[str, str] = {}
        self._session_automation_policies: dict[str, dict[str, Any] | None] = {}
        self._last_task_plan_guard_issues: list[str] = []
        self._last_task_plan_guard_fixed: bool = False
        # Cumulative tokens consumed by each session, surfaced to UI clients
        # via progress / response metadata so users can monitor usage against
        # their automation token budget. Cleared when the session is reset
        # (e.g. via the /new slash command). Lives in memory only; restoring
        # a session after engine restart starts the counter back at zero.
        self._session_tokens_used: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Run-mode / agent-profile / automation-policy helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_run_mode(value: object) -> str:
        """Normalize UI run mode with a conservative fallback."""
        if isinstance(value, str):
            mode = value.strip().lower()
            if mode in {"manual", "auto"}:
                return mode
        return "manual"

    @staticmethod
    def _parse_run_mode(value: object) -> str | None:
        """Parse run mode, returning None when absent/invalid."""
        if isinstance(value, str):
            mode = value.strip().lower()
            if mode in {"manual", "auto"}:
                return mode
        return None

    @staticmethod
    def _parse_agent_profile(value: object) -> str | None:
        """Parse agent profile, returning None when absent/invalid."""
        if isinstance(value, str):
            profile = value.strip().lower()
            if profile in {"engineer", "default", "research"}:
                return profile
        return None

    @staticmethod
    def _parse_automation_policy(value: object) -> dict[str, Any] | None:
        """Parse automation policy, returning None when absent/invalid."""
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
                raw_target = item.get("value")
                if not isinstance(metric, str) or not metric.strip():
                    continue
                if not isinstance(operator, str) or operator not in {">", ">=", "<", "<=", "=="}:
                    continue
                try:
                    target = float(raw_target)
                except (TypeError, ValueError):
                    continue
                goals.append({
                    "metric": metric.strip(),
                    "operator": operator,
                    "value": target,
                })

        max_experiments = value.get("maxExperiments")
        if not isinstance(max_experiments, int) or max_experiments <= 0:
            max_experiments = None

        max_tokens = value.get("maxTokens")
        if not isinstance(max_tokens, int) or max_tokens <= 0:
            max_tokens = None

        if not goals and max_experiments is None and max_tokens is None:
            return None

        parsed: dict[str, Any] = {"logic": logic, "goals": goals}
        if max_experiments is not None:
            parsed["maxExperiments"] = max_experiments
        if max_tokens is not None:
            parsed["maxTokens"] = max_tokens
        return parsed

    def _resolve_session_run_mode(self, session_key: str, inbound_value: object) -> str:
        """Resolve effective mode for a session, updating cache if explicitly provided."""
        explicit = self._parse_run_mode(inbound_value)
        if explicit:
            self._session_run_modes[session_key] = explicit
            return explicit
        return self._session_run_modes.get(session_key, "manual")

    def _resolve_session_agent_profile(self, session_key: str, inbound_value: object) -> str:
        """Resolve effective agent profile, updating cache if explicitly provided."""
        explicit = self._parse_agent_profile(inbound_value)
        if explicit:
            self._session_agent_profiles[session_key] = explicit
            return explicit
        return self._session_agent_profiles.get(session_key, "default")

    def _resolve_session_automation_policy(
        self,
        session_key: str,
        inbound_value: object,
    ) -> dict[str, Any] | None:
        """Resolve automation policy for a session, updating cache when provided."""
        if inbound_value is not None:
            parsed = self._parse_automation_policy(inbound_value)
            self._session_automation_policies[session_key] = parsed
            return parsed
        return self._session_automation_policies.get(session_key)

    def _accumulate_session_tokens(self, session_key: str, delta: int) -> int:
        """Add ``delta`` to the session's running token total and return the new total."""
        if delta <= 0:
            return self._session_tokens_used.get(session_key, 0)
        new_total = self._session_tokens_used.get(session_key, 0) + delta
        self._session_tokens_used[session_key] = new_total
        return new_total

    def _max_tokens_from_policy(self, policy: dict[str, Any] | None) -> int | None:
        """Return the auto-stop token budget if configured as a positive int."""
        if not isinstance(policy, dict):
            return None
        value = policy.get("maxTokens")
        if isinstance(value, int) and value > 0:
            return value
        return None

    @staticmethod
    def _agent_profile_to_agents_filename(profile: str) -> str:
        """Map profile to its AGENTS bootstrap file."""
        if profile == "engineer":
            return "AGENTS_EG.md"
        if profile == "research":
            return "AGENTS_RS.md"
        return "AGENTS.md"

    # ------------------------------------------------------------------
    # Heuristic content classifiers
    # ------------------------------------------------------------------

    @staticmethod
    def _looks_like_user_input_request(text: str | None) -> bool:
        """Heuristic: detect when assistant explicitly needs user input."""
        if not text:
            return False
        lowered = text.lower()
        keywords = (
            "please provide",
            "please confirm",
            "please choose",
            "could you",
            "can you provide",
            "which option",
            "clarify",
            "need your input",
            "what would you like to do next",
            "需要你",
            "请提供",
            "请确认",
            "请选择",
            "是否继续",
            "是否开始",
            "是否要我",
            "要我现在",
        )
        return any(k in lowered for k in keywords)

    @staticmethod
    def _looks_like_failure_response(text: str | None) -> bool:
        """Heuristic: detect blocking errors where auto should stop.

        IMPORTANT: Do not treat ordinary experiment outcomes like "hypothesis failed"
        as blocking failures. We only stop on explicit runtime/system blockage.
        """
        if not text:
            return False
        lowered = text.lower()
        hard_signals = (
            "traceback (most recent call last)",
            "sorry, i encountered an error",
            "memory archival failed",
            "command timed out",
            "exit code:",
            "permission denied",
            "no such file or directory",
            "module not found",
            "failed to connect",
            "failed to load",
            "tool call failed",
            "unrecoverable",
            "blocked by",
            "无法继续",
            "出现错误",
            "运行时错误",
        )
        if any(k in lowered for k in hard_signals):
            return True
        if lowered.startswith("error:") or "\nerror:" in lowered:
            return True
        return False

    # ------------------------------------------------------------------
    # task_plan loaders / inspectors
    # ------------------------------------------------------------------

    @staticmethod
    def _load_task_plan(project_dir: str | None) -> dict | None:
        """Load web task_plan.json if available."""
        if not project_dir:
            return None
        plan_path = Path(project_dir) / "task_plan.json"
        if not plan_path.is_file():
            return None
        try:
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _plan_has_pending_work(plan: dict | None) -> bool:
        """Return whether task_plan still has pending/running experiments."""
        if not plan:
            return False
        experiments = plan.get("experiments")
        if not isinstance(experiments, list):
            return False
        return any(
            isinstance(exp, dict) and exp.get("status") in {"pending", "running"}
            for exp in experiments
        )

    @staticmethod
    def _plan_experiment_index(plan: dict | None) -> dict[str, dict[str, Any]]:
        """Build experiment lookup by id from task_plan payload."""
        if not plan:
            return {}
        experiments = plan.get("experiments")
        if not isinstance(experiments, list):
            return {}

        index: dict[str, dict[str, Any]] = {}
        for exp in experiments:
            if not isinstance(exp, dict):
                continue
            exp_id = exp.get("id")
            if not isinstance(exp_id, str):
                continue
            normalized = exp_id.strip()
            if not normalized:
                continue
            index[normalized] = exp
        return index

    @staticmethod
    def _running_experiment_ids(plan: dict | None) -> list[str]:
        """Return ids of currently running experiments."""
        if not plan:
            return []
        experiments = plan.get("experiments")
        if not isinstance(experiments, list):
            return []

        ids: list[str] = []
        for exp in experiments:
            if not isinstance(exp, dict) or exp.get("status") != "running":
                continue
            exp_id = exp.get("id")
            if isinstance(exp_id, str) and exp_id.strip():
                ids.append(exp_id.strip())
        return ids

    @classmethod
    def _has_experiment_checkpoint_update(
        cls,
        before_plan: dict | None,
        after_plan: dict | None,
    ) -> bool:
        """Check whether running experiments were persisted in task_plan this round."""
        running_ids = cls._running_experiment_ids(before_plan)
        if not running_ids:
            return True

        before_index = cls._plan_experiment_index(before_plan)
        after_index = cls._plan_experiment_index(after_plan)

        for exp_id in running_ids:
            before_entry = before_index.get(exp_id)
            after_entry = after_index.get(exp_id)
            # Entry disappeared or changed => task plan checkpoint advanced.
            if after_entry is None:
                return True
            if before_entry != after_entry:
                return True
        return False

    @classmethod
    def _experiments_crossed_boundary(
        cls,
        before_plan: dict | None,
        after_plan: dict | None,
    ) -> list[str]:
        """Return experiment ids that moved from active to terminal in one round."""
        before_index = cls._plan_experiment_index(before_plan)
        after_index = cls._plan_experiment_index(after_plan)
        terminal_statuses = {"completed", "failed", "skipped"}
        active_statuses = {"pending", "running"}

        crossed: list[str] = []
        for exp_id, after_entry in after_index.items():
            if not isinstance(after_entry, dict):
                continue
            after_status = after_entry.get("status")
            if after_status not in terminal_statuses:
                continue
            before_entry = before_index.get(exp_id)
            before_status = before_entry.get("status") if isinstance(before_entry, dict) else None
            if before_status in active_statuses or before_status is None:
                crossed.append(exp_id)
        return crossed

    @staticmethod
    def _to_number(value: object) -> float | None:
        """Convert metric value to float when possible."""
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return None
        return None

    @classmethod
    def _collect_latest_plan_metrics(cls, plan: dict | None) -> dict[str, float]:
        """Collect latest numeric metrics from completed experiments."""
        if not plan:
            return {}
        experiments = plan.get("experiments")
        if not isinstance(experiments, list):
            return {}

        metrics: dict[str, float] = {}
        for exp in experiments:
            if not isinstance(exp, dict) or exp.get("status") != "completed":
                continue
            results = exp.get("results")
            metric_map = results.get("metrics") if isinstance(results, dict) else None
            if not isinstance(metric_map, dict):
                continue
            for metric_name, raw_value in metric_map.items():
                if not isinstance(metric_name, str) or not metric_name.strip():
                    continue
                numeric = cls._to_number(raw_value)
                if numeric is None:
                    continue
                metrics[metric_name.strip()] = numeric
        return metrics

    @staticmethod
    def _count_completed_experiments(plan: dict | None) -> int:
        """Count completed experiments in task plan."""
        if not plan:
            return 0
        experiments = plan.get("experiments")
        if not isinstance(experiments, list):
            return 0
        return sum(1 for exp in experiments if isinstance(exp, dict) and exp.get("status") == "completed")

    @staticmethod
    def _compare_goal(metric_value: float, operator: str, target: float) -> bool:
        """Evaluate one metric threshold predicate."""
        if operator == ">":
            return metric_value > target
        if operator == ">=":
            return metric_value >= target
        if operator == "<":
            return metric_value < target
        if operator == "<=":
            return metric_value <= target
        if operator == "==":
            return abs(metric_value - target) <= 1e-9
        return False

    @classmethod
    def _evaluate_automation_stop_policy(
        cls,
        policy: dict[str, Any] | None,
        *,
        plan: dict | None,
        tokens_used: int,
    ) -> str | None:
        """Return stop reason if auto-stop policy threshold is reached."""
        if not policy:
            return None

        goals = policy.get("goals") if isinstance(policy.get("goals"), list) else []
        if goals:
            metrics = cls._collect_latest_plan_metrics(plan)
            evaluations: list[bool] = []
            for goal in goals:
                if not isinstance(goal, dict):
                    continue
                metric = goal.get("metric")
                operator = goal.get("operator")
                target = cls._to_number(goal.get("value"))
                if not isinstance(metric, str) or not metric.strip() or not isinstance(operator, str) or target is None:
                    continue
                metric_value = metrics.get(metric.strip())
                evaluations.append(
                    metric_value is not None and cls._compare_goal(metric_value, operator, target)
                )
            if evaluations:
                logic = str(policy.get("logic", "AND")).upper()
                goals_met = all(evaluations) if logic == "AND" else any(evaluations)
                if goals_met:
                    return "automation goals reached"

        max_experiments = policy.get("maxExperiments")
        if isinstance(max_experiments, int) and max_experiments > 0:
            completed = cls._count_completed_experiments(plan)
            if completed >= max_experiments:
                return f"max experiments reached ({completed}/{max_experiments})"

        max_tokens = policy.get("maxTokens")
        if isinstance(max_tokens, int) and max_tokens > 0 and tokens_used >= max_tokens:
            return f"token budget reached ({tokens_used}/{max_tokens})"

        return None

    # ------------------------------------------------------------------
    # task_plan.result restore guard
    # ------------------------------------------------------------------

    @staticmethod
    def _plan_result_state(plan: dict | None) -> tuple[bool, Any]:
        """Return whether `result` exists and its payload."""
        if not isinstance(plan, dict):
            return False, None
        if "result" not in plan:
            return False, None
        return True, plan.get("result")

    @classmethod
    def _has_result_section_update(
        cls,
        before_plan: dict | None,
        after_plan: dict | None,
    ) -> bool:
        """Detect whether task_plan.result changed between rounds."""
        return cls._plan_result_state(before_plan) != cls._plan_result_state(after_plan)

    @staticmethod
    def _looks_like_result_request(content: object, metadata: dict[str, Any] | None = None) -> bool:
        """Detect explicit user intent to generate/export final deliverables."""
        if isinstance(metadata, dict) and bool(metadata.get("_allow_result_write")):
            return True
        if not isinstance(content, str):
            return False
        lowered = content.lower()
        if "manual export request for" in lowered:
            return True
        if "final deliverable" in lowered and "request" in lowered:
            return True
        if "导出" in content and ("报告" in content or "论文" in content or "结果" in content):
            return True
        return False

    def _restore_result_section(
        self,
        project_dir: str | None,
        *,
        before_plan: dict | None,
        after_plan: dict | None,
    ) -> tuple[dict | None, bool]:
        """Restore result section to previous state and persist task_plan."""
        if not project_dir or not isinstance(after_plan, dict):
            return after_plan, False
        if not self._has_result_section_update(before_plan, after_plan):
            return after_plan, False

        has_before_result, before_result = self._plan_result_state(before_plan)
        patched = json.loads(json.dumps(after_plan, ensure_ascii=False))
        if has_before_result:
            patched["result"] = before_result
        else:
            patched.pop("result", None)

        plan_path = Path(project_dir) / "task_plan.json"
        try:
            plan_path.write_text(
                json.dumps(patched, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            return after_plan, False
        return patched, True

    def _restore_completion_status(
        self,
        project_dir: str | None,
        *,
        after_plan: dict | None,
    ) -> tuple[dict | None, bool]:
        """Keep project status in progress until a final result is explicitly available."""
        if not project_dir or not isinstance(after_plan, dict):
            return after_plan, False
        if after_plan.get("status") != "completed":
            return after_plan, False
        if plan_has_final_result_output(after_plan.get("result")) and not self._plan_has_pending_work(after_plan):
            return after_plan, False

        patched = json.loads(json.dumps(after_plan, ensure_ascii=False))
        patched["status"] = "in_progress"
        plan_path = Path(project_dir) / "task_plan.json"
        try:
            plan_path.write_text(
                json.dumps(patched, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            return after_plan, False
        return patched, True

    # ------------------------------------------------------------------
    # task_plan guardrails / contract hints
    # ------------------------------------------------------------------

    def _guard_task_plan_structure(
        self,
        project_dir: str | None,
        *,
        auto_fix: bool = True,
        profile: str | None = None,
    ) -> bool:
        """Apply task_plan guardrails before auto-continue rounds."""
        if not project_dir:
            self._last_task_plan_guard_issues = []
            self._last_task_plan_guard_fixed = False
            return True
        result = guard_task_plan_file(Path(project_dir), auto_fix=auto_fix, profile=profile)
        issues = list(result.get("issues") or [])
        self._last_task_plan_guard_issues = issues
        self._last_task_plan_guard_fixed = bool(result.get("fixed"))
        if result.get("fixed"):
            logger.info("task_plan guardrails auto-fixed {}", project_dir)
        if result.get("blocking"):
            logger.warning(
                "task_plan guardrails blocked auto-continue for {}: {}",
                project_dir,
                issues[:3],
            )
            return False
        return True

    @staticmethod
    def _load_project_contract_version(project_dir: str | None) -> int:
        """Load project contract version from .mira/project.json."""
        if not project_dir:
            return 1
        meta_path = Path(project_dir) / ".mira" / "project.json"
        if not meta_path.is_file():
            return 1
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 1
        if isinstance(payload, dict):
            value = payload.get("contract_version")
            if isinstance(value, int) and value in {1, 2}:
                return value
        return 1

    def _build_task_plan_contract_hint(
        self,
        *,
        project_dir: str | None,
        agent_profile: str | None,
    ) -> str:
        """Build a concise contract hint for auto-run task_plan updates."""
        profile = self._parse_agent_profile(agent_profile) or "default"
        contract_version = self._load_project_contract_version(project_dir)
        contract = get_task_plan_contract(
            profile=profile,
            contract_version=contract_version,
        )
        required_completed = contract.get("required_completed_fields") or []
        required_falsify = contract.get("required_falsify_fields") or []

        lines = [
            "Task-plan contract requirements (enforce in this write):",
            f"- profile={profile}, contract_version={contract_version}",
        ]
        if required_completed:
            lines.append(
                "- when setting status=completed, include required fields: "
                + ", ".join(str(item) for item in required_completed)
            )
        if required_falsify:
            lines.append(
                "- when conclusion indicates rejection/failure, also include: "
                + ", ".join(str(item) for item in required_falsify)
            )
        lines.append(
            "- do not mark an experiment as completed unless required contract fields are present."
        )
        return "\n".join(lines)

    def _is_strict_contract_enforced(
        self, *, project_dir: str | None, agent_profile: str | None
    ) -> bool:
        """Whether current project is in strict contract mode with required fields."""
        profile = self._parse_agent_profile(agent_profile) or "default"
        contract_version = self._load_project_contract_version(project_dir)
        contract = get_task_plan_contract(
            profile=profile,
            contract_version=contract_version,
        )
        return (
            contract_version >= 2
            and bool(contract.get("required_completed_fields"))
        )

    # ------------------------------------------------------------------
    # Auto-continue prompt builders
    # ------------------------------------------------------------------

    def _build_auto_continue_message(
        self,
        channel: str,
        chat_id: str,
        project_dir: str | None,
        run_mode: str,
        agent_profile: str | None = None,
    ) -> str:
        """Build the synthetic internal continue message for server-side auto mode."""
        runtime_ctx = ContextBuilder._build_runtime_context(
            channel,
            chat_id,
            project_dir,
            run_mode=run_mode,
        )
        contract_hint = self._build_task_plan_contract_hint(
            project_dir=project_dir,
            agent_profile=agent_profile,
        )
        return (
            f"{runtime_ctx}\n\n{self._AUTO_CONTINUE_MARKER}\n"
            "Auto-run checkpoint requirements:\n"
            "1) If you just finished an experiment, immediately update and write task_plan.json "
            "(set status/results/conclusion/next for that experiment) BEFORE starting the next one.\n"
            "2) Execute exactly ONE pending experiment in this round, then return control. "
            "If no pending experiment exists but automation goals are still unmet and "
            "maxExperiments budget remains, first append the next sequential pending "
            "experiment(s) to task_plan.json, execute exactly ONE of them, then return control.\n"
            "3) Do not stop for confirmation unless user input is strictly required.\n\n"
            f"{contract_hint}"
        )

    def _build_auto_guardrail_repair_message(
        self,
        *,
        channel: str,
        chat_id: str,
        project_dir: str | None,
        run_mode: str,
        issues: list[str],
    ) -> str:
        """Build an internal message asking model to patch blocked plan fields."""
        runtime_ctx = ContextBuilder._build_runtime_context(
            channel,
            chat_id,
            project_dir,
            run_mode=run_mode,
        )
        issue_lines = "\n".join(f"- {item}" for item in issues[:8]) if issues else "- unknown issue"
        return (
            f"{runtime_ctx}\n\n{self._AUTO_CONTINUE_MARKER}\n"
            "Guardrail validation blocked task_plan progression. "
            "Patch task_plan.json to satisfy the missing required fields only.\n"
            "Do NOT rewrite prior conclusions or metrics unless logically necessary.\n"
            "Use concrete evidence from experiment artifacts/results; "
            "placeholder text like 'Guardrail auto-fill: ...' is invalid in strict mode.\n"
            f"Missing/invalid items:\n{issue_lines}"
        )

    def _build_auto_checkpoint_sync_message(
        self,
        *,
        channel: str,
        chat_id: str,
        project_dir: str | None,
        run_mode: str,
        running_ids: list[str],
        agent_profile: str | None = None,
    ) -> str:
        """Build an internal message that forces per-experiment task_plan checkpointing."""
        runtime_ctx = ContextBuilder._build_runtime_context(
            channel,
            chat_id,
            project_dir,
            run_mode=run_mode,
        )
        contract_hint = self._build_task_plan_contract_hint(
            project_dir=project_dir,
            agent_profile=agent_profile,
        )
        items = "\n".join(f"- {item}" for item in running_ids[:8]) if running_ids else "- running experiment"
        return (
            f"{runtime_ctx}\n\n{self._AUTO_CONTINUE_MARKER}\n"
            "Checkpoint barrier: task_plan.json still shows the same running experiment(s) as before this round.\n"
            "Before any new work, update and write task_plan.json now for the current running experiment(s):\n"
            "1) set final status (completed/failed/skipped) if finished;\n"
            "2) persist results/conclusion/next (or progress if still running);\n"
            "3) then stop this turn.\n"
            f"Running experiments to sync:\n{items}\n\n"
            f"{contract_hint}"
        )

    def _should_continue_auto_web(
        self,
        *,
        channel: str,
        run_mode: str,
        project_dir: str | None,
        final_content: str | None,
        auto_round: int,
        agent_profile: str | None = None,
        automation_policy: dict[str, Any] | None = None,
        tokens_used: int = 0,
    ) -> bool:
        """Decide whether to schedule another internal auto-run cycle."""
        if channel != "web" or run_mode != "auto":
            return False
        if not self._guard_task_plan_structure(project_dir, profile=agent_profile):
            return False
        if auto_round >= self._AUTO_MAX_ROUNDS:
            logger.warning("Auto mode max rounds ({}) reached", self._AUTO_MAX_ROUNDS)
            return False
        if self._looks_like_failure_response(final_content):
            return False
        if self._looks_like_user_input_request(final_content):
            return False
        plan = self._load_task_plan(project_dir)
        if self._plan_has_pending_work(plan):
            return True
        return self._should_replan_exhausted_queue(
            automation_policy,
            plan=plan,
            tokens_used=tokens_used,
        )

    @classmethod
    def _should_replan_exhausted_queue(
        cls,
        policy: dict[str, Any] | None,
        *,
        plan: dict | None,
        tokens_used: int,
    ) -> bool:
        """Continue auto mode when the queue is empty but experiment budget remains."""
        if not policy or cls._plan_has_pending_work(plan):
            return False
        max_experiments = policy.get("maxExperiments")
        if not isinstance(max_experiments, int) or max_experiments <= 0:
            return False
        if cls._evaluate_automation_stop_policy(policy, plan=plan, tokens_used=tokens_used):
            return False
        return cls._count_completed_experiments(plan) < max_experiments

    # ------------------------------------------------------------------
    # Control / session lifecycle overrides
    # ------------------------------------------------------------------

    async def _handle_control(self, msg: InboundMessage, control: str) -> bool:
        """Handle research-specific control messages (currently ``set_mode``)."""
        if control == "set_mode":
            await self._handle_set_mode(msg)
            return True
        return await super()._handle_control(msg, control)

    async def _handle_set_mode(self, msg: InboundMessage) -> None:
        """Update session run mode immediately without entering normal dispatch."""
        mode = self._normalize_run_mode((msg.metadata or {}).get("run_mode"))
        self._session_run_modes[msg.session_key] = mode
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=f"Run mode switched to {mode}.",
            metadata={
                "_control": "set_mode_ack",
                "run_mode": mode,
            },
        ))

    def _on_session_reset(self, session_key: str) -> None:
        """Drop research-specific per-session caches on /new."""
        super()._on_session_reset(session_key)
        self._session_automation_policies.pop(session_key, None)
        self._session_tokens_used.pop(session_key, None)

    # ------------------------------------------------------------------
    # _process_message override (research orchestration)
    # ------------------------------------------------------------------

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        audit_hook: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            model_runtime = self._get_model_runtime(key)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
            history = session.get_history(max_messages=self.memory_window)
            messages = self.context.build_messages(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
            )
            final_content, _, all_msgs = await self._run_agent_loop(messages, model_runtime=model_runtime)
            self._save_turn(session, all_msgs, 1 + len(history))
            self.sessions.save(session)
            return OutboundMessage(channel=channel, chat_id=chat_id,
                                  content=final_content or "Background task completed.")

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        meta = msg.metadata or {}
        project_dir = meta.get("project_dir")
        key = session_key or msg.session_key
        run_mode = self._resolve_session_run_mode(key, meta.get("run_mode"))
        agent_profile = self._resolve_session_agent_profile(key, meta.get("agent_profile"))
        automation_policy = self._resolve_session_automation_policy(
            key,
            meta.get("automation_policy"),
        )
        agents_filename = self._agent_profile_to_agents_filename(agent_profile)
        if project_dir:
            sessions_mgr = self._get_project_sessions(project_dir)
        else:
            sessions_mgr = self.sessions

        session = sessions_mgr.get_or_create(key)
        memory_workspace = Path(project_dir) if project_dir else self.workspace
        recent_skill_names: list[str] = []
        if isinstance(session.metadata, dict):
            raw_recent = session.metadata.get("_recent_skills")
            if isinstance(raw_recent, list):
                recent_skill_names = [str(s) for s in raw_recent if isinstance(s, str)]

        # Slash commands
        cmd = msg.content.strip().lower()
        if cmd == "/new":
            if msg.channel == "cli":
                snapshot = session.messages[session.last_consolidated:]
                session.clear()
                sessions_mgr.save(session)
                sessions_mgr.invalidate(session.key)
                self._on_session_reset(session.key)
                if snapshot:
                    self._schedule_background(self.consolidator.archive(snapshot))
                return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                      content="New session started.")
            ok = await self._consolidate_memory(session, archive_all=True, workspace_override=memory_workspace)
            if not ok:
                return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                      content="Memory archival failed. Session was not reset.")
            session.clear()
            sessions_mgr.save(session)
            sessions_mgr.invalidate(session.key)
            self._on_session_reset(session.key)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="New session started.")
        if cmd == "/help":
            ctx = CommandContext(
                msg=msg,
                session=session,
                key=key,
                raw=msg.content.strip(),
                loop=self,
            )
            handled = await self._command_router.dispatch(ctx)
            if handled is not None:
                return handled

        if cmd.startswith("/"):
            ctx = CommandContext(
                msg=msg,
                session=session,
                key=key,
                raw=msg.content.strip(),
                loop=self,
            )
            handled = await self._command_router.dispatch(ctx)
            if handled is not None:
                return handled

        unconsolidated = len(session.messages) - session.last_consolidated
        if (unconsolidated >= self.memory_window and session.key not in self._consolidating):
            self._consolidating.add(session.key)
            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())
            _mw = memory_workspace

            async def _consolidate_and_unlock():
                try:
                    async with lock:
                        await self._consolidate_memory(session, workspace_override=_mw)
                finally:
                    self._consolidating.discard(session.key)
                    _task = asyncio.current_task()
                    if _task is not None:
                        self._consolidation_tasks.discard(_task)

            _task = asyncio.create_task(_consolidate_and_unlock())
            self._consolidation_tasks.add(_task)

        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"))
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        await self.consolidator.maybe_consolidate_by_tokens(session)
        history = session.get_history(max_messages=self.memory_window)
        model_runtime = self._get_model_runtime(key)
        extra_system = self._compose_extra_system(
            meta.get("_ui_system_instructions"),
            meta.get("_task_plan_guard_notice"),
        )

        ctx = ContextBuilder(memory_workspace) if project_dir else self.context
        suggested_skills = ctx.skills.suggest_skills(
            msg.content,
            recent=recent_skill_names,
            limit=3,
        )
        active_skills: list[str] = []
        for name in [*recent_skill_names, *suggested_skills]:
            if name not in active_skills:
                active_skills.append(name)
        active_skills = active_skills[-4:]
        skill_hint = ""
        if suggested_skills:
            skill_hint = (
                "Skill routing hint: this request likely matches one or more skills. "
                "Before answering, use read_file to inspect these SKILL.md files if relevant:\n"
                + "\n".join(f"- {name}" for name in suggested_skills)
            )
            if on_progress:
                try:
                    await on_progress(
                        f"skill router -> {', '.join(suggested_skills)}",
                        tool_hint=True,
                    )
                except TypeError:
                    await on_progress(f"skill router -> {', '.join(suggested_skills)}")
        if extra_system:
            extra_system = skill_hint + "\n\n" + extra_system if skill_hint else extra_system
        else:
            extra_system = skill_hint or None
        initial_messages = ctx.build_messages(
            history=history,
            current_message=msg.content,
            skill_names=active_skills or None,
            media=msg.media if msg.media else None,
            channel=msg.channel, chat_id=msg.chat_id,
            project_dir=project_dir,
            run_mode=run_mode,
            agents_filename=agents_filename,
            extra_system=extra_system,
        )

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            progress_meta = dict(msg.metadata or {})
            progress_meta["_progress"] = True
            progress_meta["_tool_hint"] = tool_hint
            progress_meta["tokens_used_session"] = self._session_tokens_used.get(key, 0)
            max_tokens = self._max_tokens_from_policy(automation_policy)
            if max_tokens is not None:
                progress_meta["max_tokens"] = max_tokens
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=progress_meta,
            ))

        progress_cb = on_progress or _bus_progress
        current_turn_skills: set[str] = set()
        audit_cb = None
        emit_audit_to_channel = msg.channel == "web" or bool(meta.get("_emit_skill_audit"))
        allow_result_write = self._looks_like_result_request(msg.content, meta)
        if emit_audit_to_channel or audit_hook:
            async def _audit(details: dict[str, Any]) -> None:
                skill_name = details.get("skill_name")
                if isinstance(skill_name, str) and skill_name.strip():
                    current_turn_skills.add(skill_name.strip())
                if audit_hook:
                    await audit_hook(details)
                if not emit_audit_to_channel:
                    return
                metadata = dict(msg.metadata or {})
                metadata["_audit_only"] = True
                metadata["_audit_event"] = "skill_invoked"
                metadata["_audit_details"] = details
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="",
                    metadata=metadata,
                ))

            audit_cb = _audit
        run_kwargs: dict[str, Any] = {
            "model_runtime": model_runtime,
            "on_progress": progress_cb,
            "audit_hook": audit_cb,
        }
        if on_stream is not None:
            run_kwargs["on_stream"] = on_stream
        if on_stream_end is not None:
            run_kwargs["on_stream_end"] = on_stream_end
        round_plan_before = self._load_task_plan(project_dir) if msg.channel == "web" else None
        final_content, _, all_msgs = await self._run_agent_loop(initial_messages, **run_kwargs)
        total_tokens_used = self._last_loop_tokens_used
        self._accumulate_session_tokens(key, self._last_loop_tokens_used)
        round_plan_after = self._load_task_plan(project_dir) if msg.channel == "web" else None
        if msg.channel == "web" and not allow_result_write:
            round_plan_after, restored = self._restore_result_section(
                project_dir,
                before_plan=round_plan_before,
                after_plan=round_plan_after,
            )
            if restored:
                await progress_cb(
                    "auto-run guard: skipped task_plan.result update without explicit export request"
                )
            round_plan_after, status_restored = self._restore_completion_status(
                project_dir,
                after_plan=round_plan_after,
            )
            if status_restored:
                await progress_cb(
                    "auto-run guard: kept task_plan.status=in_progress until explicit export request"
                )

        auto_round = 0
        guard_repair_round = 0
        checkpoint_repair_round = 0
        while True:
            current_mode = self._session_run_modes.get(key, run_mode)
            automation_policy = self._resolve_session_automation_policy(key, None)
            if msg.channel == "web" and current_mode == "auto":
                crossed = self._experiments_crossed_boundary(round_plan_before, round_plan_after)
                if len(crossed) > 1:
                    await progress_cb(
                        "auto-run guard warning: multiple experiments advanced in one round "
                        f"({', '.join(crossed[:3])})"
                    )
                if crossed and project_dir:
                    code_guard = self._guard_task_plan_structure(
                        project_dir,
                        auto_fix=True,
                        profile=agent_profile,
                    )
                    if code_guard and self._last_task_plan_guard_fixed:
                        await progress_cb(
                            "auto-run guard: code-level contract normalization applied "
                            "after experiment transition"
                        )
                        round_plan_after = self._load_task_plan(project_dir)
                if not self._has_experiment_checkpoint_update(round_plan_before, round_plan_after):
                    if checkpoint_repair_round >= self._AUTO_CHECKPOINT_REPAIR_MAX:
                        await progress_cb(
                            "auto-run guard warning: task_plan checkpoint missing after experiment round; "
                            "continuing due auto mode"
                        )
                    else:
                        checkpoint_repair_round += 1
                        auto_round += 1
                        running_ids = self._running_experiment_ids(round_plan_before)
                        await progress_cb(
                            f"auto-run checkpoint repair {checkpoint_repair_round}: "
                            "forcing task_plan sync for running experiment"
                        )
                        all_msgs.append({
                            "role": "user",
                            "content": self._build_auto_checkpoint_sync_message(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                project_dir=project_dir,
                                run_mode=current_mode,
                                running_ids=running_ids,
                                agent_profile=agent_profile,
                            ),
                        })
                        round_plan_before = round_plan_after
                        final_content, _, all_msgs = await self._run_agent_loop(
                            all_msgs,
                            model_runtime=model_runtime,
                            on_progress=progress_cb,
                            audit_hook=audit_cb,
                        )
                        total_tokens_used += self._last_loop_tokens_used
                        self._accumulate_session_tokens(key, self._last_loop_tokens_used)
                        round_plan_after = self._load_task_plan(project_dir)
                        if msg.channel == "web" and not allow_result_write:
                            round_plan_after, restored = self._restore_result_section(
                                project_dir,
                                before_plan=round_plan_before,
                                after_plan=round_plan_after,
                            )
                            if restored:
                                await progress_cb(
                                    "auto-run guard: skipped task_plan.result update without explicit export request"
                                )
                            round_plan_after, status_restored = self._restore_completion_status(
                                project_dir,
                                after_plan=round_plan_after,
                            )
                            if status_restored:
                                await progress_cb(
                                    "auto-run guard: kept task_plan.status=in_progress until explicit export request"
                                )
                        continue

            if msg.channel == "web" and current_mode == "auto":
                current_plan = self._load_task_plan(project_dir)
                stop_reason = self._evaluate_automation_stop_policy(
                    automation_policy,
                    plan=current_plan,
                    tokens_used=total_tokens_used,
                )
                if stop_reason:
                    await progress_cb(f"auto-run stop condition: {stop_reason}")
                    break

            should_continue = self._should_continue_auto_web(
                channel=msg.channel,
                run_mode=current_mode,
                project_dir=project_dir,
                final_content=final_content,
                auto_round=auto_round,
                agent_profile=agent_profile,
                automation_policy=automation_policy,
                tokens_used=total_tokens_used,
            )
            if not should_continue:
                guard_issues = list(getattr(self, "_last_task_plan_guard_issues", []))
                continue_despite_guard = False
                if (
                    msg.channel == "web"
                    and current_mode == "auto"
                    and guard_issues
                    and not self._looks_like_failure_response(final_content)
                    and not self._looks_like_user_input_request(final_content)
                ):
                    if guard_repair_round < self._AUTO_GUARD_REPAIR_MAX:
                        guard_repair_round += 1
                        auto_round += 1
                        await progress_cb(
                            f"auto-run guardrail repair {guard_repair_round}: "
                            "filling required evidence fields"
                        )
                        all_msgs.append({
                            "role": "user",
                            "content": self._build_auto_guardrail_repair_message(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                project_dir=project_dir,
                                run_mode=current_mode,
                                issues=guard_issues,
                            ),
                        })
                        guard_plan_before = round_plan_after if msg.channel == "web" else None
                        final_content, _, all_msgs = await self._run_agent_loop(all_msgs, **run_kwargs)
                        total_tokens_used += self._last_loop_tokens_used
                        self._accumulate_session_tokens(key, self._last_loop_tokens_used)
                        round_plan_after = self._load_task_plan(project_dir)
                        round_plan_before = guard_plan_before
                        if msg.channel == "web" and not allow_result_write:
                            round_plan_after, restored = self._restore_result_section(
                                project_dir,
                                before_plan=guard_plan_before,
                                after_plan=round_plan_after,
                            )
                            if restored:
                                await progress_cb(
                                    "auto-run guard: skipped task_plan.result update without explicit export request"
                                )
                            round_plan_after, status_restored = self._restore_completion_status(
                                project_dir,
                                after_plan=round_plan_after,
                            )
                            if status_restored:
                                await progress_cb(
                                    "auto-run guard: kept task_plan.status=in_progress until explicit export request"
                                )
                        continue
                    has_pending = self._plan_has_pending_work(self._load_task_plan(project_dir))
                    strict_contract = self._is_strict_contract_enforced(
                        project_dir=project_dir,
                        agent_profile=agent_profile,
                    )
                    if strict_contract and has_pending and auto_round < self._AUTO_MAX_ROUNDS:
                        guard_repair_round = 0
                        auto_round += 1
                        await progress_cb(
                            "auto-run strict contract repair: required fields still missing; "
                            "requesting targeted completion before next experiment"
                        )
                        all_msgs.append({
                            "role": "user",
                            "content": self._build_auto_guardrail_repair_message(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                project_dir=project_dir,
                                run_mode=current_mode,
                                issues=guard_issues,
                            ),
                        })
                        guard_plan_before = round_plan_after if msg.channel == "web" else None
                        final_content, _, all_msgs = await self._run_agent_loop(all_msgs, **run_kwargs)
                        total_tokens_used += self._last_loop_tokens_used
                        self._accumulate_session_tokens(key, self._last_loop_tokens_used)
                        round_plan_after = self._load_task_plan(project_dir)
                        round_plan_before = guard_plan_before
                        if msg.channel == "web" and not allow_result_write:
                            round_plan_after, restored = self._restore_result_section(
                                project_dir,
                                before_plan=guard_plan_before,
                                after_plan=round_plan_after,
                            )
                            if restored:
                                await progress_cb(
                                    "auto-run guard: skipped task_plan.result update without explicit export request"
                                )
                            round_plan_after, status_restored = self._restore_completion_status(
                                project_dir,
                                after_plan=round_plan_after,
                            )
                            if status_restored:
                                await progress_cb(
                                    "auto-run guard: kept task_plan.status=in_progress until explicit export request"
                                )
                        continue
                    if has_pending and auto_round < self._AUTO_MAX_ROUNDS:
                        continue_despite_guard = True
                        await progress_cb(
                            "auto-run guard warning: contract issues remain after repair; "
                            "continuing and deferring strict cleanup"
                        )
                if not continue_despite_guard:
                    break
            run_mode = current_mode
            auto_round += 1
            await progress_cb(
                f"auto-run round {auto_round}: continuing to next experiment cycle"
            )
            all_msgs.append({
                "role": "user",
                "content": self._build_auto_continue_message(
                    msg.channel,
                    msg.chat_id,
                    project_dir,
                    run_mode,
                    agent_profile,
                ),
            })
            round_plan_before = round_plan_after
            final_content, _, all_msgs = await self._run_agent_loop(all_msgs, **run_kwargs)
            total_tokens_used += self._last_loop_tokens_used
            self._accumulate_session_tokens(key, self._last_loop_tokens_used)
            round_plan_after = self._load_task_plan(project_dir)
            if msg.channel == "web" and not allow_result_write:
                round_plan_after, restored = self._restore_result_section(
                    project_dir,
                    before_plan=round_plan_before,
                    after_plan=round_plan_after,
                )
                if restored:
                    await progress_cb(
                        "auto-run guard: skipped task_plan.result update without explicit export request"
                    )
                round_plan_after, status_restored = self._restore_completion_status(
                    project_dir,
                    after_plan=round_plan_after,
                )
                if status_restored:
                    await progress_cb(
                        "auto-run guard: kept task_plan.status=in_progress until explicit export request"
                    )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        if isinstance(session.metadata, dict):
            prior = session.metadata.get("_recent_skills")
            merged: list[str] = []
            if isinstance(prior, list):
                for item in prior:
                    if isinstance(item, str) and item not in merged:
                        merged.append(item)
            for item in sorted(current_turn_skills):
                if item not in merged:
                    merged.append(item)
            session.metadata["_recent_skills"] = merged[-10:]

        self._save_turn(session, all_msgs, 1 + len(history))
        sessions_mgr.save(session)

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)
        response_metadata = dict(msg.metadata or {})
        response_metadata["tokens_used_session"] = self._session_tokens_used.get(key, 0)
        max_tokens = self._max_tokens_from_policy(automation_policy)
        if max_tokens is not None:
            response_metadata["max_tokens"] = max_tokens
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=final_content,
            metadata=response_metadata,
        )


__all__ = ["ResearchAgentLoop"]
