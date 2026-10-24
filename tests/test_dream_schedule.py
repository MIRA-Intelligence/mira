"""Dream runs on a schedule (it was never registered, so USER.md never grew)."""

from __future__ import annotations

from mira_engine.agent.tools.cron import CronTool
from mira_engine.config.schema import Config, DreamConfig
from mira_engine.cron.service import CronService
from mira_engine.cron.system_jobs import DREAM_JOB_ID, register_dream_job, run_system_job
from mira_engine.cron.types import CronJob


def test_dream_is_scheduled_every_two_hours_by_default(tmp_path):
    cron = CronService(tmp_path / "jobs.json")
    job = register_dream_job(cron, Config().agents.defaults.dream, "UTC")
    assert job.id == DREAM_JOB_ID and job.enabled
    assert job.schedule.kind == "every" and job.schedule.every_ms == 2 * 3_600_000
    assert job.state.next_run_at_ms is not None
    # Persisted, so it survives a restart, and registered once.
    register_dream_job(CronService(tmp_path / "jobs.json"), DreamConfig(), "UTC")
    assert [j.id for j in CronService(tmp_path / "jobs.json").list_jobs(include_disabled=True)] == [DREAM_JOB_ID]


def test_a_changed_interval_replaces_the_stored_schedule(tmp_path):
    cron = CronService(tmp_path / "jobs.json")
    register_dream_job(cron, DreamConfig(interval_h=2), "UTC")
    register_dream_job(cron, DreamConfig(interval_h=6), "UTC")
    assert cron.get_job(DREAM_JOB_ID).schedule.every_ms == 6 * 3_600_000


def test_the_dream_job_cannot_be_removed_by_the_agent(tmp_path):
    cron = CronService(tmp_path / "jobs.json")
    register_dream_job(cron, DreamConfig(), "UTC")
    assert "cannot be removed" in CronTool(cron)._remove_job(DREAM_JOB_ID)
    assert cron.get_job(DREAM_JOB_ID) is not None


async def test_only_the_dream_job_runs_dream():
    calls: list[str] = []

    async def dream() -> bool:
        calls.append("dream")
        return True

    assert await run_system_job(CronJob(id=DREAM_JOB_ID, name="dream"), dream) is True
    assert await run_system_job(CronJob(id="user-job", name="remind me"), dream) is False
    assert calls == ["dream"]
