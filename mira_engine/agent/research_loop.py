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

from mira_engine.agent.base_loop import UNIFIED_SESSION_KEY, BaseAgentLoop
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

        # ``strictHeuristics`` (default True) lets long-running auto sessions
        # opt out of the user-input / failure keyword heuristics when only
        # hard guards (max rounds / max tokens / max experiments / explicit
        # tool failures) should decide when to stop. We track whether the
        # caller set the field so we can persist the policy even when no
        # other goals/budgets are configured.
        strict_raw = value.get("strictHeuristics")
        strict_explicit = isinstance(strict_raw, bool)
        strict_heuristics = strict_raw if strict_explicit else True

        if (
            not goals
            and max_experiments is None
            and max_tokens is None
            and not strict_explicit
        ):
            return None

        parsed: dict[str, Any] = {
            "logic": logic,
            "goals": goals,
            "strictHeuristics": strict_heuristics,
        }
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
    def _strict_heuristics_from_policy(policy: dict[str, Any] | None) -> bool:
        """Return whether the user-input / failure heuristics should fire.

        Defaults to True (current behaviour). Setting
        ``automation_policy.strictHeuristics = false`` lets a long auto run
        rely solely on hard guards (round / experiment / token budgets and
        explicit tool failures), which matters when the model's natural
        prose keeps tripping the keyword heuristics.
        """
        if isinstance(policy, dict):
            value = policy.get("strictHeuristics")
            if isinstance(value, bool):
                return value
        return True

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

    # Closing-paragraph window used by the user-input / failure heuristics.
    # A genuine "blocked, please advise" message almost always lands in the
    # last paragraph of the assistant turn; matching mid-text was the main
    # source of false positives in auto mode where the model would casually
    # mention "could you" / "请确认" inside a summary and the loop would
    # treat that as a hard stop.
    _AUTO_HEURISTIC_TAIL_CHARS = 600

    @classmethod
    def _heuristic_tail(cls, text: str) -> str:
        """Return the closing window of ``text`` used by stop heuristics."""
        last_block = text.rsplit("\n\n", 1)[-1]
        if len(last_block) > cls._AUTO_HEURISTIC_TAIL_CHARS:
            return last_block[-cls._AUTO_HEURISTIC_TAIL_CHARS:]
        return last_block

    @classmethod
    def _looks_like_user_input_request(cls, text: str | None) -> bool:
        """Heuristic: detect when the assistant explicitly needs user input.

        Tightened in PR 1: only inspects the closing paragraph and uses a
        conservative keyword list. Generic phrases like ``could you`` /
        ``clarify`` / ``需要你`` appearing in mid-response prose are NOT
        halts — they used to over-trigger and stop auto mode for no reason.
        """
        if not text:
            return False
        tail = cls._heuristic_tail(text).lower()
        keywords = (
            # English: explicit asks, deliberately conservative.
            "please confirm",
            "please choose",
            "please provide",
            "could you provide",
            "could you confirm",
            "can you provide",
            "need your input",
            "awaiting your input",
            "awaiting your confirmation",
            "what would you like to do next",
            "shall i proceed",
            "should i proceed",
            "do you want me to",
            # Chinese: keep only phrasings that genuinely block on the user.
            "请提供",
            "请确认",
            "请选择",
            "是否继续",
            "是否开始",
            "是否要我",
            "等待你的确认",
            "等待用户",
        )
        return any(k in tail for k in keywords)

    @classmethod
    def _looks_like_failure_response(cls, text: str | None) -> bool:
        """Heuristic: detect blocking system errors where auto should stop.

        Tightened in PR 1: we only stop on errors that the agent surface
        itself cannot recover from (tracebacks bubbled to the assistant,
        memory archival failure, tool-call failure, and explicit "I can't
        continue" verdicts in the closing paragraph).

        Ordinary experiment-level signals MUST NOT trigger here:
        - ``exit code:`` / ``module not found`` / ``no such file or
          directory`` / ``permission denied`` legitimately appear in stdout
          dumps and in analysis text while the model is debugging.
        - ``hypothesis failed`` / ``实验失败`` / ``出现错误`` are valid
          experiment outcomes that auto mode should keep iterating on.
        """
        if not text:
            return False
        lowered = text.lower()
        hard_signals = (
            "traceback (most recent call last)",
            "sorry, i encountered an error",
            "memory archival failed",
            "tool call failed",
            "unrecoverable error",
            "无法继续",
        )
        if any(k in lowered for k in hard_signals):
            return True
        tail = cls._heuristic_tail(text).lower()
        soft_signals = (
            "i'm unable to proceed",
            "i am unable to proceed",
            "cannot proceed because",
            "cannot continue because",
            "i cannot continue",
            "blocked by ",
        )
        return any(k in tail for k in soft_signals)

    @classmethod
    def _looks_like_llm_provider_error(cls, text: str | None) -> bool:
        """Detect when ``final_content`` is a system-level LLM call failure.

        These are surfaced by provider error handlers (``_handle_error`` in
        ``anthropic_provider`` / ``azure_openai_provider`` / etc., and the
        chain-failure path in ``RoutedProviderManager.chat``) and must always
        halt auto mode — they are NOT experiment outcomes. Without this
        guard a parameter-level 4xx (e.g. Azure dropping ``temperature``)
        gets retried every round until ``_AUTO_MAX_ROUNDS`` (20) is
        exhausted.

        Unlike :meth:`_looks_like_failure_response`, this check fires
        regardless of ``strictHeuristics`` because the agent has not even
        produced a turn — there is nothing to iterate on.
        """
        if not text:
            return False
        lowered = text.lower()
        markers = (
            # Provider wrappers (see anthropic_provider._handle_error,
            # azure_openai_provider._handle_error, openai_compat_provider,
            # litellm_provider.chat, openai_codex_provider).
            "error calling llm",
            "error calling azure openai",
            "error calling codex",
            "error calling github copilot",
            # Underlying SDK / gateway error types.
            "litellm.badrequesterror",
            "azure_aiexception",
            "invalid_request_error",
            "bad_request_error",
            # RoutedProviderManager terminal message.
            "all candidate models failed for this turn",
            # base_loop fallback when an error response has no content.
            "sorry, i encountered an error calling the ai model",
        )
        return any(marker in lowered for marker in markers)

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
            "If no pending experiment exists but the project's research goals are still "
            "unmet (or any automation budget remains), first append the next sequential "
            "pending experiment(s) to task_plan.json, execute exactly ONE of them, then "
            "return control.\n"
            "3) Do NOT stop for confirmation. The user is not in the loop on this turn — "
            "auto mode keeps running until a hard guard (round / experiment / token "
            "budget, guardrail block, or explicit tool failure) fires. If a logical next "
            "step exists, perform it. Only ask the user when you are blocked by data or "
            "credentials that only the user can supply.\n"
            "4) Do NOT end your reply with a question to the user. Avoid auto-mode "
            "anti-patterns such as 'shall I proceed?', 'do you want me to ...?', "
            "'是否继续?', '是否要我...?', '请确认...?'. State the conclusion of this "
            "round and the concrete next action you will take.\n\n"
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

    def _evaluate_continuation(
        self,
        *,
        run_mode: str,
        project_dir: str | None,
        final_content: str | None,
        auto_round: int,
        agent_profile: str | None = None,
        automation_policy: dict[str, Any] | None = None,
        tokens_used: int = 0,
    ) -> tuple[bool, str | None]:
        """Decide whether to schedule another auto-run cycle, with reason.

        Returns ``(should_continue, stop_reason)``. ``stop_reason`` is a short
        machine-readable label suitable for inclusion in progress events so
        the user can tell *why* auto mode stopped without grepping logs.
        ``stop_reason`` is ``None`` when the loop continues, and ``None``
        when the call is a silent no-op (non-auto run mode).

        Note: there is no longer a channel filter here. ``ResearchAgentLoop``
        is the only class wiring auto mode in, so any channel reaching this
        method is by definition the research surface and should be honoured
        uniformly. The basic agent loop never calls this method.
        """
        if run_mode != "auto":
            return False, None
        # LLM provider errors must halt the loop unconditionally — the agent
        # never even produced a turn, so the next round will hit the exact
        # same failure (parameter rejected, auth invalid, gateway down, ...).
        # Without this guard a single bad request burns through all 20
        # auto-run rounds before surfacing the error to the user.
        if self._looks_like_llm_provider_error(final_content):
            logger.warning("Auto mode halting: LLM provider error in final response")
            return False, "llm provider error"
        if not self._guard_task_plan_structure(project_dir, profile=agent_profile):
            return False, "task_plan guardrail blocking"
        if auto_round >= self._AUTO_MAX_ROUNDS:
            logger.warning("Auto mode max rounds ({}) reached", self._AUTO_MAX_ROUNDS)
            return False, f"max rounds reached ({self._AUTO_MAX_ROUNDS})"
        strict_heuristics = self._strict_heuristics_from_policy(automation_policy)
        if strict_heuristics and self._looks_like_failure_response(final_content):
            return False, "failure heuristic matched"
        if strict_heuristics and self._looks_like_user_input_request(final_content):
            return False, "user-input heuristic matched"
        plan = self._load_task_plan(project_dir)
        if self._plan_has_pending_work(plan):
            return True, None
        if self._should_replan_exhausted_queue(
            automation_policy,
            plan=plan,
            tokens_used=tokens_used,
        ):
            return True, None
        return False, "queue exhausted, no replan condition met"

    def _should_continue_auto_ui(
        self,
        *,
        run_mode: str,
        project_dir: str | None,
        final_content: str | None,
        auto_round: int,
        agent_profile: str | None = None,
        automation_policy: dict[str, Any] | None = None,
        tokens_used: int = 0,
    ) -> bool:
        """Boolean wrapper around :meth:`_evaluate_continuation`.

        Name kept for backward compatibility with downstream call sites
        even though the ``_ui`` suffix is now historical — research auto
        mode no longer requires the UI channel.
        """
        decision, _ = self._evaluate_continuation(
            run_mode=run_mode,
            project_dir=project_dir,
            final_content=final_content,
            auto_round=auto_round,
            agent_profile=agent_profile,
            automation_policy=automation_policy,
            tokens_used=tokens_used,
        )
        return decision

    @classmethod
    def _should_replan_exhausted_queue(
        cls,
        policy: dict[str, Any] | None,
        *,
        plan: dict | None,
        tokens_used: int,
    ) -> bool:
        """Continue auto mode when the queue is empty but more work is warranted.

        Liberalised in PR 2 so that auto mode does not silently halt the
        moment the model forgets to append the next experiment:

        - With pending work in the plan, never replan (caller handles it).
        - With no policy at all, replan — ``_AUTO_MAX_ROUNDS`` already bounds
          the runaway and ``strictHeuristics`` still gates the heuristics.
        - With a policy whose stop conditions (goals / maxExperiments /
          maxTokens) are already met, do not replan.
        - With a policy that has goals not yet reached, replan regardless of
          whether ``maxExperiments`` is configured (previously we only
          replanned when ``maxExperiments`` was set, which silently dropped
          goal-driven sessions).
        - With a policy whose ``maxExperiments`` budget still has room,
          replan up to that budget.
        - With a policy that only carries ``maxTokens`` /
          ``strictHeuristics`` and no goals/budget, default to replanning;
          ``maxTokens`` and ``_AUTO_MAX_ROUNDS`` keep the loop bounded.
        """
        if cls._plan_has_pending_work(plan):
            return False
        if policy is None:
            return True
        if cls._evaluate_automation_stop_policy(policy, plan=plan, tokens_used=tokens_used):
            return False
        goals = policy.get("goals") if isinstance(policy.get("goals"), list) else []
        if goals:
            return True
        max_experiments = policy.get("maxExperiments")
        if isinstance(max_experiments, int) and max_experiments > 0:
            return cls._count_completed_experiments(plan) < max_experiments
        return True

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
        meta = msg.metadata or {}
        mode = self._normalize_run_mode(meta.get("run_mode"))
        project_ref = self._project_ref_from_metadata(meta)
        raw_key = (
            UNIFIED_SESSION_KEY
            if getattr(self, "_unified_session", False) and not msg.session_key_override
            else msg.session_key
        )
        self._session_run_modes[self._scoped_session_key(project_ref, raw_key)] = mode
        response_metadata = {
            **dict(meta),
            "_control": "set_mode_ack",
            "run_mode": mode,
        }
        if project_ref is not None:
            response_metadata["project_id"] = project_ref.project_id
            response_metadata["project_dir"] = str(project_ref.project_dir)
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=f"Run mode switched to {mode}.",
            metadata=response_metadata,
        ))

    def _on_session_reset(self, session_key: str) -> None:
        """Drop volatile research state on /new while retaining UI selections."""
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
            meta = msg.metadata or {}
            project_ref = self._project_ref_from_metadata(meta)
            raw_key = f"{channel}:{chat_id}"
            key = self._scoped_session_key(project_ref, raw_key)
            sessions_mgr = self._get_project_sessions(project_ref) if project_ref else self.sessions
            session = sessions_mgr.get_or_create(raw_key)
            model_runtime = self._get_model_runtime(key)
            self._set_tool_context(
                channel,
                chat_id,
                meta.get("message_id"),
                project_ref=project_ref,
                session_key=key,
            )
            history = session.get_history(max_messages=self.memory_window)
            ctx = self._get_project_context(project_ref)
            messages = ctx.build_messages(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
                project_dir=str(project_ref.project_dir) if project_ref else None,
            )
            final_content, _, all_msgs = await self._run_agent_loop(messages, model_runtime=model_runtime)
            self._save_turn(session, all_msgs, 1 + len(history))
            sessions_mgr.save(session)
            return OutboundMessage(channel=channel, chat_id=chat_id,
                                  content=final_content or "Background task completed.")

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        meta = msg.metadata or {}
        project_ref = self._project_ref_from_metadata(meta)
        project_dir = str(project_ref.project_dir) if project_ref else None
        raw_key = session_key or msg.session_key
        key = self._scoped_session_key(project_ref, raw_key)
        run_mode = self._resolve_session_run_mode(key, meta.get("run_mode"))
        agent_profile = self._resolve_session_agent_profile(key, meta.get("agent_profile"))
        automation_policy = self._resolve_session_automation_policy(
            key,
            meta.get("automation_policy"),
        )
        agents_filename = self._agent_profile_to_agents_filename(agent_profile)
        sessions_mgr = self._get_project_sessions(project_ref) if project_ref else self.sessions

        session = sessions_mgr.get_or_create(raw_key)
        memory_workspace = project_ref.project_dir if project_ref else self.workspace
        ctx = self._get_project_context(project_ref)
        project_consolidator = self._get_project_consolidator(project_ref, sessions_mgr, ctx)
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
                self._on_session_reset(key)
                if snapshot:
                    self._schedule_background(project_consolidator.archive(snapshot))
                return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                      content="New session started.")
            ok = await self._consolidate_memory(session, archive_all=True, workspace_override=memory_workspace)
            if not ok:
                return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                      content="Memory archival failed. Session was not reset.")
            session.clear()
            sessions_mgr.save(session)
            sessions_mgr.invalidate(session.key)
            self._on_session_reset(key)
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
        if (unconsolidated >= self.memory_window and key not in self._consolidating):
            self._consolidating.add(key)
            lock = self._consolidation_locks.setdefault(key, asyncio.Lock())
            _mw = memory_workspace

            async def _consolidate_and_unlock():
                try:
                    async with lock:
                        await self._consolidate_memory(session, workspace_override=_mw)
                finally:
                    self._consolidating.discard(key)
                    _task = asyncio.current_task()
                    if _task is not None:
                        self._consolidation_tasks.discard(_task)

            _task = asyncio.create_task(_consolidate_and_unlock())
            self._consolidation_tasks.add(_task)

        self._set_tool_context(
            msg.channel,
            msg.chat_id,
            meta.get("message_id"),
            project_ref=project_ref,
            session_key=key,
        )
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        await project_consolidator.maybe_consolidate_by_tokens(session)
        history = session.get_history(max_messages=self.memory_window)
        model_runtime = self._get_model_runtime(key)
        extra_system = self._compose_extra_system(
            meta.get("_ui_system_instructions"),
            meta.get("_task_plan_guard_notice"),
        )

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
        emit_audit_to_channel = msg.channel == "ui" or bool(meta.get("_emit_skill_audit"))
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
        round_plan_before = self._load_task_plan(project_dir)
        final_content, _, all_msgs = await self._run_agent_loop(initial_messages, **run_kwargs)
        total_tokens_used = self._last_loop_tokens_used
        self._accumulate_session_tokens(key, self._last_loop_tokens_used)
        round_plan_after = self._load_task_plan(project_dir)
        if msg.channel == "ui" and not allow_result_write:
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
            if current_mode == "auto":
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
                        if msg.channel == "ui" and not allow_result_write:
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

            if current_mode == "auto":
                current_plan = self._load_task_plan(project_dir)
                stop_reason = self._evaluate_automation_stop_policy(
                    automation_policy,
                    plan=current_plan,
                    tokens_used=total_tokens_used,
                )
                if stop_reason:
                    await progress_cb(f"auto-run stop condition: {stop_reason}")
                    break

            should_continue, continuation_reason = self._evaluate_continuation(
                run_mode=current_mode,
                project_dir=project_dir,
                final_content=final_content,
                auto_round=auto_round,
                agent_profile=agent_profile,
                automation_policy=automation_policy,
                tokens_used=total_tokens_used,
            )
            if not should_continue:
                if current_mode == "auto" and continuation_reason:
                    await progress_cb(
                        f"auto-run stop reason: {continuation_reason}"
                    )
                guard_issues = list(getattr(self, "_last_task_plan_guard_issues", []))
                continue_despite_guard = False
                strict_heuristics = self._strict_heuristics_from_policy(automation_policy)
                heuristic_block = strict_heuristics and (
                    self._looks_like_failure_response(final_content)
                    or self._looks_like_user_input_request(final_content)
                )
                if (
                    current_mode == "auto"
                    and guard_issues
                    and not heuristic_block
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
                        guard_plan_before = round_plan_after
                        final_content, _, all_msgs = await self._run_agent_loop(all_msgs, **run_kwargs)
                        total_tokens_used += self._last_loop_tokens_used
                        self._accumulate_session_tokens(key, self._last_loop_tokens_used)
                        round_plan_after = self._load_task_plan(project_dir)
                        round_plan_before = guard_plan_before
                        if msg.channel == "ui" and not allow_result_write:
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
                        guard_plan_before = round_plan_after
                        final_content, _, all_msgs = await self._run_agent_loop(all_msgs, **run_kwargs)
                        total_tokens_used += self._last_loop_tokens_used
                        self._accumulate_session_tokens(key, self._last_loop_tokens_used)
                        round_plan_after = self._load_task_plan(project_dir)
                        round_plan_before = guard_plan_before
                        if msg.channel == "ui" and not allow_result_write:
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
            if msg.channel == "ui" and not allow_result_write:
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
