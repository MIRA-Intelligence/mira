"""Cron tool for scheduling reminders and tasks."""

from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from medpilot.agent.tools.base import Tool
from medpilot.cron.service import CronService
from medpilot.cron.types import CronSchedule


class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks."""

    def __init__(self, cron_service: CronService, default_timezone: str = "UTC"):
        self._cron = cron_service
        self._channel = ""
        self._chat_id = ""
        self._in_cron_context: ContextVar[bool] = ContextVar("cron_in_context", default=False)
        self._default_timezone = default_timezone

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current session context for delivery."""
        self._channel = channel
        self._chat_id = chat_id

    def set_cron_context(self, active: bool):
        """Mark whether the tool is executing inside a cron job callback."""
        return self._in_cron_context.set(active)

    def reset_cron_context(self, token) -> None:
        """Restore previous cron context."""
        self._in_cron_context.reset(token)

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return "Schedule reminders and recurring tasks. Actions: add, list, remove."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "remove"],
                    "description": "Action to perform",
                },
                "message": {"type": "string", "description": "Reminder message (for add)"},
                "every_seconds": {
                    "type": "integer",
                    "description": "Interval in seconds (for recurring tasks)",
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression like '0 9 * * *' (for scheduled tasks)",
                },
                "tz": {
                    "type": "string",
                    "description": "IANA timezone for cron expressions (e.g. 'America/Vancouver')",
                },
                "at": {
                    "type": "string",
                    "description": "ISO datetime for one-time execution (e.g. '2026-02-12T10:30:00')",
                },
                "job_id": {"type": "string", "description": "Job ID (for remove)"},
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        name: str | None = None,
        message: str = "",
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        tz: str | None = None,
        at: str | None = None,
        job_id: str | None = None,
        deliver: bool = True,
        **kwargs: Any,
    ) -> str:
        if action == "add":
            if self._in_cron_context.get():
                return "Error: cannot schedule new jobs from within a cron job execution"
            return self._add_job(name, message, every_seconds, cron_expr, tz, at, deliver=deliver)
        elif action == "list":
            return self._list_jobs()
        elif action == "remove":
            return self._remove_job(job_id)
        return f"Unknown action: {action}"

    def _add_job(
        self,
        name: str | None,
        message: str,
        every_seconds: int | None,
        cron_expr: str | None,
        tz: str | None,
        at: str | None,
        deliver: bool = True,
    ) -> str:
        if not message:
            return "Error: message is required for add"
        if not self._channel or not self._chat_id:
            return "Error: no session context (channel/chat_id)"
        if tz and not cron_expr:
            return "Error: tz can only be used with cron_expr"
        if tz:
            from zoneinfo import ZoneInfo

            try:
                ZoneInfo(tz)
            except (KeyError, Exception):
                return f"Error: unknown timezone '{tz}'"

        # Build schedule
        delete_after = False
        if every_seconds:
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz or self._default_timezone)
        elif at:
            try:
                dt = datetime.fromisoformat(at)
            except ValueError:
                return f"Error: invalid ISO datetime format '{at}'. Expected format: YYYY-MM-DDTHH:MM:SS"
            if dt.tzinfo is None:
                from zoneinfo import ZoneInfo

                dt = dt.replace(tzinfo=ZoneInfo(self._default_timezone))
            dt = dt.astimezone(timezone.utc)
            at_ms = int(dt.timestamp() * 1000)
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after = True
        else:
            return "Error: either every_seconds, cron_expr, or at is required"

        job = self._cron.add_job(
            name=(name or message[:30]),
            schedule=schedule,
            message=message,
            deliver=deliver,
            channel=self._channel,
            to=self._chat_id,
            delete_after_run=delete_after,
        )
        return f"Created job '{job.name}' (id: {job.id})"

    def _format_timing(self, schedule: CronSchedule) -> str:
        if schedule.kind == "cron" and schedule.expr:
            return f"cron: {schedule.expr} ({schedule.tz})" if schedule.tz else f"cron: {schedule.expr}"
        if schedule.kind == "every" and schedule.every_ms:
            ms = schedule.every_ms
            if ms % 3_600_000 == 0:
                return f"every {ms // 3_600_000}h"
            if ms % 60_000 == 0:
                return f"every {ms // 60_000}m"
            if ms >= 1_000:
                return f"every {ms // 1_000}s"
            return f"every {ms}ms"
        if schedule.kind == "at" and schedule.at_ms:
            tz_name = self._default_timezone
            from zoneinfo import ZoneInfo

            dt = datetime.fromtimestamp(schedule.at_ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz_name))
            return f"at {dt.strftime('%Y-%m-%d %H:%M:%S')} ({tz_name})"
        return schedule.kind

    def _format_state(self, state, schedule: CronSchedule) -> list[str]:
        lines: list[str] = []
        tz_name = schedule.tz or self._default_timezone
        from zoneinfo import ZoneInfo

        if state.last_run_at_ms:
            dt = datetime.fromtimestamp(state.last_run_at_ms / 1000, tz=timezone.utc).astimezone(
                ZoneInfo(tz_name)
            )
            status = state.last_status or "unknown"
            suffix = f" - {state.last_error}" if state.last_error else ""
            lines.append(f"  Last run: {dt.strftime('%Y-%m-%d %H:%M:%S')} ({tz_name}) - {status}{suffix}")
        if state.next_run_at_ms:
            dt = datetime.fromtimestamp(state.next_run_at_ms / 1000, tz=timezone.utc).astimezone(
                ZoneInfo(tz_name)
            )
            lines.append(f"  Next run: {dt.strftime('%Y-%m-%d %H:%M:%S')} ({tz_name})")
        return lines

    def _list_jobs(self) -> str:
        jobs = self._cron.list_jobs()
        if not jobs:
            return "No scheduled jobs."
        lines: list[str] = []
        for j in jobs:
            lines.append(f"- {j.name} (id: {j.id}, {self._format_timing(j.schedule)})")
            if j.id == "dream":
                lines.append("  Dream memory consolidation for long-term memory.")
                lines.append("  This job is protected and cannot be removed.")
            lines.extend(self._format_state(j.state, j.schedule))
        return "Scheduled jobs:\n" + "\n".join(lines)

    def _remove_job(self, job_id: str | None) -> str:
        if not job_id:
            return "Error: job_id is required for remove"
        removed = self._cron.remove_job(job_id)
        if removed == "protected":
            return (
                f"Cannot remove job `{job_id}`. "
                "Dream memory consolidation job for long-term memory cannot be removed."
            )
        if removed:
            return f"Removed job {job_id}"
        return f"Job {job_id} not found"
