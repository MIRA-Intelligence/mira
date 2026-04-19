from __future__ import annotations

from dataclasses import dataclass

from medpilot.agent.tools.cron import CronTool
from medpilot.cron.types import CronJob, CronJobState, CronPayload, CronSchedule


@dataclass
class _FakeCronService:
    added: list[CronJob]
    removed_ids: set[str]

    def __init__(self):
        self.added = []
        self.removed_ids = set()
        self.jobs: list[CronJob] = []

    def add_job(self, **kwargs):
        job = CronJob(
            id="job-1",
            name=kwargs["name"],
            schedule=kwargs["schedule"],
            payload=CronPayload(
                message=kwargs["message"],
                deliver=kwargs["deliver"],
                channel=kwargs["channel"],
                to=kwargs["to"],
            ),
            state=CronJobState(),
            delete_after_run=kwargs.get("delete_after_run", False),
        )
        self.added.append(job)
        self.jobs.append(job)
        return job

    def list_jobs(self):
        return self.jobs

    def remove_job(self, job_id: str) -> bool:
        if job_id in self.removed_ids:
            return False
        self.removed_ids.add(job_id)
        self.jobs = [j for j in self.jobs if j.id != job_id]
        return True


def test_cron_tool_metadata() -> None:
    tool = CronTool(_FakeCronService())
    assert tool.name == "cron"
    assert "Schedule reminders" in tool.description
    assert tool.parameters["required"] == ["action"]


async def test_cron_add_requires_context_and_message() -> None:
    tool = CronTool(_FakeCronService())
    assert await tool.execute(action="add", message="", every_seconds=5) == "Error: message is required for add"
    assert await tool.execute(action="add", message="hi", every_seconds=5) == "Error: no session context (channel/chat_id)"


async def test_cron_add_valid_every_and_list_and_remove() -> None:
    svc = _FakeCronService()
    tool = CronTool(svc)
    tool.set_context("web", "PRJ-1")

    created = await tool.execute(action="add", message="ping", every_seconds=3)
    assert "Created job 'ping'" in created
    assert svc.added[0].schedule == CronSchedule(kind="every", every_ms=3000)

    listed = await tool.execute(action="list")
    assert "Scheduled jobs:" in listed
    assert "job-1" in listed

    removed = await tool.execute(action="remove", job_id="job-1")
    assert removed == "Removed job job-1"
    missing = await tool.execute(action="remove", job_id="job-1")
    assert missing == "Job job-1 not found"


async def test_cron_add_rejects_invalid_tz_and_at() -> None:
    tool = CronTool(_FakeCronService())
    tool.set_context("web", "PRJ-1")
    assert await tool.execute(action="add", message="x", tz="UTC") == "Error: tz can only be used with cron_expr"
    assert "unknown timezone" in await tool.execute(
        action="add", message="x", cron_expr="* * * * *", tz="Not/AZone"
    )
    assert "invalid ISO datetime format" in await tool.execute(
        action="add", message="x", at="bad-date"
    )
    assert await tool.execute(action="add", message="x") == "Error: either every_seconds, cron_expr, or at is required"


async def test_cron_add_inside_cron_context_is_blocked() -> None:
    tool = CronTool(_FakeCronService())
    tool.set_context("web", "PRJ-1")
    token = tool.set_cron_context(True)
    try:
        result = await tool.execute(action="add", message="x", every_seconds=1)
        assert result == "Error: cannot schedule new jobs from within a cron job execution"
    finally:
        tool.reset_cron_context(token)


async def test_cron_unknown_action_and_remove_without_id() -> None:
    tool = CronTool(_FakeCronService())
    assert await tool.execute(action="noop") == "Unknown action: noop"
    assert await tool.execute(action="remove") == "Error: job_id is required for remove"
