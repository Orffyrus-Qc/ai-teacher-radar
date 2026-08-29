"""Scheduling.

An n8n instance already runs on this machine, so n8n owns the schedule: its
cron nodes POST to /jobs and the queue here does the GPU-aware sequencing.

SCHEDULER_MODE controls what happens when n8n is *not* driving:
  n8n      - never self-schedule. If the workflow is off, nothing runs.
  internal - ignore n8n entirely and self-schedule.
  auto     - (default) self-schedule as a dead-man's switch: at each slot, wait
             N8N_GRACE_MIN, and only fire if no n8n-sourced job showed up. That
             way importing the workflow later costs nothing and removing it does
             not silently stop the radar.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import config, jobs, store

log = logging.getLogger("radar.scheduler")

_sched: BackgroundScheduler | None = None


def _n8n_recently_fired(kind: str, within_min: int) -> bool:
    last = store.last_job_from("n8n")
    if not last or last["kind"] != kind:
        return False
    try:
        created = datetime.fromisoformat(last["created_at"])
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created <= timedelta(minutes=within_min)


def _fire(kind: str, slot: str | None) -> None:
    if config.SCHEDULER_MODE == "auto" and _n8n_recently_fired(kind, config.N8N_GRACE_MIN * 2):
        log.info("skipping internal %s (%s): n8n already covered it", kind, slot)
        return
    log.info("internal scheduler firing %s (%s)", kind, slot)
    jobs.submit(kind, slot=slot, origin="internal")


def start() -> BackgroundScheduler | None:
    global _sched
    if config.SCHEDULER_MODE == "n8n":
        log.info("SCHEDULER_MODE=n8n - internal cron disabled, orchestrator owns the schedule")
        return None

    _sched = BackgroundScheduler(timezone=config.TZ)
    grace = config.N8N_GRACE_MIN if config.SCHEDULER_MODE == "auto" else 0

    for slot in config.SEARCH_SLOTS:
        hh, mm = (int(x) for x in slot.split(":"))
        fire = (datetime(2000, 1, 1, hh, mm) + timedelta(minutes=grace)).time()
        _sched.add_job(_fire, CronTrigger(hour=fire.hour, minute=fire.minute),
                       args=["search", slot], id=f"search-{slot}", replace_existing=True,
                       misfire_grace_time=3600)

    hh, mm = (int(x) for x in config.DAILY_SYNTHESIS_AT.split(":"))
    _sched.add_job(_fire, CronTrigger(hour=hh, minute=mm), args=["daily", None],
                   id="daily", replace_existing=True, misfire_grace_time=3600)

    hh, mm = (int(x) for x in config.WEEKLY_SYNTHESIS_AT.split(":"))
    _sched.add_job(_fire, CronTrigger(day_of_week=config.WEEKLY_SYNTHESIS_DOW, hour=hh, minute=mm),
                   args=["weekly", None], id="weekly", replace_existing=True,
                   misfire_grace_time=7200)

    _sched.start()
    log.info("internal scheduler started (mode=%s, slots=%s, +%dmin grace)",
             config.SCHEDULER_MODE, config.SEARCH_SLOTS, grace)
    return _sched


def upcoming() -> list[dict]:
    if not _sched:
        return []
    return [{"id": j.id, "next_run": j.next_run_time.isoformat() if j.next_run_time else None}
            for j in _sched.get_jobs()]


def shutdown() -> None:
    if _sched:
        _sched.shutdown(wait=False)
