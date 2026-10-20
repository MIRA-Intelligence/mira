"""Built-in jobs the engine schedules for itself.

Dream distils conversation history into ``USER.md`` / ``MEMORY.md`` / ``SOUL.md``.
It only ever ran when someone typed ``/dream``: ``agents.defaults.dream`` (every
2h by default) existed but nothing scheduled it, so the user profile never grew.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from mira_engine.cron.types import CronJob, CronPayload

if TYPE_CHECKING:
    from mira_engine.config.schema import DreamConfig
    from mira_engine.cron.service import CronService

DREAM_JOB_ID = "dream"


def register_dream_job(cron: "CronService", dream: "DreamConfig", timezone: str) -> CronJob:
    """Schedule Dream (protected: the cron tool cannot remove it)."""
    job = cron.register_system_job(
        CronJob(
            id=DREAM_JOB_ID,
            name="dream",
            schedule=dream.build_schedule(timezone),
            payload=CronPayload(kind="system_event", message="Dream memory consolidation"),
        )
    )
    logger.info("Dream memory consolidation scheduled ({})", dream.describe_schedule())
    return job


async def run_system_job(job: CronJob, dream_run: Callable[[], Awaitable[Any]]) -> bool:
    """Run ``job`` if it is a built-in job. Returns False for user jobs.

    Dream returns at once without a model call when there is no new history, so
    a short interval costs nothing while the engine is idle.
    """
    if job.id != DREAM_JOB_ID:
        return False
    try:
        did_work = await dream_run()
        logger.info("Dream run finished ({})", "updated memory" if did_work else "nothing new")
    except Exception:  # noqa: BLE001 - a failed run must not take the scheduler down
        logger.exception("Dream run failed")
        raise
    return True
