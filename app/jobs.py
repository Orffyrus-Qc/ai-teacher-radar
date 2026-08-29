"""Single-worker job queue with a GPU gate.

Only one job runs at a time — the point is to *avoid* contending for the GPU,
so parallelism here would defeat the purpose.

The gate is deliberately asymmetric. Harvesting is pure HTTP, so it runs even
while the GPU is pinned; only the LLM half waits. That means a search scheduled
during a six-hour video render still captures everything published in that
window — it just publishes the written brief later, and leaves a DEFERRED
notice on disk in the meantime so the wait is visible rather than silent.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config, gpu, pipeline, render, store, synthesis

log = logging.getLogger("radar.jobs")

KINDS = ("search", "daily", "weekly")

_worker: threading.Thread | None = None
_stop = threading.Event()
_state_lock = threading.Lock()
_live: dict = {"job": None, "phase": "idle", "gpu": "unknown", "since": None}


def submit(kind: str, slot: str | None = None, origin: str = "manual") -> dict:
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind!r}; expected one of {KINDS}")
    job_id = f"{kind}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    job = store.create_job(job_id, kind, slot, origin)
    log.info("queued %s (kind=%s slot=%s origin=%s)", job_id, kind, slot, origin)
    return job


def live() -> dict:
    with _state_lock:
        return dict(_live)


def _set_live(**kv) -> None:
    with _state_lock:
        _live.update(kv)


# --------------------------------------------------------------------- worker
def start_worker() -> None:
    global _worker
    if _worker and _worker.is_alive():
        return
    orphans = store.reset_orphans()
    if orphans:
        log.warning("requeued %d job(s) left running by a previous process", orphans)
    _stop.clear()
    _worker = threading.Thread(target=_loop, name="radar-worker", daemon=True)
    _worker.start()


def stop_worker() -> None:
    _stop.set()


def _loop() -> None:
    log.info("worker started")
    while not _stop.is_set():
        try:
            job = store.next_job()
            if not job:
                _set_live(job=None, phase="idle")
                _stop.wait(5)
                continue
            _run(job)
        except Exception:  # noqa: BLE001 - the worker must survive anything
            log.exception("worker iteration failed")
            _stop.wait(15)
    log.info("worker stopped")


def _run(job: dict) -> None:
    job_id, kind, slot = job["id"], job["kind"], job["slot"]
    _set_live(job=job_id, phase="starting", since=store.utcnow())
    store.update_job(job_id, state="running", started_at=job.get("started_at") or store.utcnow(),
                     attempts=job.get("attempts", 0) + 1)
    run_id = job_id
    store.start_run(run_id, kind, slot)

    harvest_state = None
    # A deferred job can outlive the process that deferred it (container
    # restart), so the notice path lives in the DB, not just this frame.
    prior = (job.get("result") or {}).get("deferred_note")
    deferred_note: Path | None = Path(prior) if prior else None
    deferred_reason = ""
    wait_started = time.monotonic()
    waited = 0.0

    try:
        # ---- GPU-free half -------------------------------------------------
        if kind == "search":
            _set_live(phase="harvesting")
            status = gpu.status()
            if status.busy and not config.FETCH_WHEN_GPU_BUSY:
                log.info("%s: GPU busy, holding harvest too", job_id)
            else:
                harvest_state = pipeline.harvest(run_id)

        # ---- GPU gate ------------------------------------------------------
        use_llm = config.LLM_ENABLED
        gpu_note = "not checked"
        if config.LLM_ENABLED:
            while not _stop.is_set():
                status = gpu.status()
                gpu_note = status.summary()
                _set_live(gpu=gpu_note)
                if not status.busy:
                    if waited:
                        log.info("%s: GPU free after %.0fs, resuming", job_id, waited)
                        gpu_note = f"free after {int(waited)}s wait ({deferred_reason or 'busy'})"
                    break

                waited = time.monotonic() - wait_started
                if waited >= config.GPU_MAX_DEFER_S:
                    log.warning("%s: GPU still busy after %.0fs, publishing without LLM",
                                job_id, waited)
                    use_llm = False
                    gpu_note = (f"still busy after {int(waited / 60)} min "
                                f"({status.summary()}) — published rule-based")
                    break

                deferred_reason = status.summary()
                _set_live(phase="deferred_gpu_busy")
                store.update_job(job_id, state="deferred_gpu_busy",
                                 gpu_reason=deferred_reason,
                                 deferred_since=job.get("deferred_since") or store.utcnow())
                store.finish_run(run_id, status="deferred", gpu_deferred=1,
                                 gpu_wait_s=int(waited), finished_at=None)
                deferred_note = _write_deferred_notice(
                    job_id, kind, slot, run_id, deferred_reason, harvest_state, waited)
                _stop.wait(config.GPU_RECHECK_S)
            else:
                return  # shutting down
        else:
            gpu_note = "LLM disabled"

        store.update_job(job_id, state="running", gpu_reason=None)
        _set_live(phase="publishing", gpu=gpu_note)

        # ---- GPU half ------------------------------------------------------
        if kind == "search":
            if harvest_state is None:
                harvest_state = pipeline.harvest(run_id)
            out_path = pipeline.publish(harvest_state, run_id=run_id,
                                        slot=slot or render.local_now().strftime("%H:%M"),
                                        use_llm=use_llm, gpu_note=gpu_note)
            new_items, total = harvest_state["new_count"], len(harvest_state["items"])
        elif kind == "daily":
            out_path = synthesis.run_daily(run_id, use_llm=use_llm, gpu_note=gpu_note)
            new_items = total = 0
        else:
            out_path = synthesis.run_weekly(run_id, use_llm=use_llm, gpu_note=gpu_note)
            new_items = total = 0

        if deferred_note and deferred_note.exists():
            deferred_note.unlink()

        result = {"output": str(out_path), "new_items": new_items,
                  "total_items": total, "gpu": gpu_note,
                  "gpu_wait_s": int(waited), "llm_used": use_llm}
        store.update_job(job_id, state="done", finished_at=store.utcnow(), result=result)
        store.finish_run(run_id, status="done", finished_at=store.utcnow(),
                         new_items=new_items, total_items=total,
                         output_path=str(out_path), gpu_wait_s=int(waited),
                         gpu_deferred=1 if waited else 0, detail=result)
        log.info("%s finished -> %s", job_id, out_path)

    except Exception as exc:  # noqa: BLE001
        log.exception("%s failed", job_id)
        store.update_job(job_id, state="failed", finished_at=store.utcnow(), error=str(exc))
        store.finish_run(run_id, status="failed", finished_at=store.utcnow(),
                         detail={"error": str(exc)})
    finally:
        _set_live(job=None, phase="idle")


def _write_deferred_notice(job_id: str, kind: str, slot: str | None, run_id: str,
                           reason: str, harvest_state: dict | None,
                           waited: float) -> Path:
    """Make the wait visible on disk, not just in the API."""
    now = render.local_now()
    day = now.strftime("%Y-%m-%d")
    folder = config.OUT_DIR / "daily" / day
    folder.mkdir(parents=True, exist_ok=True)
    stamp = (slot or now.strftime("%H:%M")).replace(":", "")
    path = folder / f"{stamp}-{kind}.DEFERRED.md"

    harvested = ""
    if harvest_state:
        harvested = (f"\nThe harvest already ran: **{len(harvest_state['items'])} relevant "
                     f"items** ({harvest_state['new_count']} new) are captured in the "
                     "database. Only the written summary is waiting.\n")

    path.write_text(
        f"# ⏸ {kind} run deferred — GPU busy\n\n"
        f"- **Scheduled slot:** {slot or now.strftime('%H:%M')} ({day})\n"
        f"- **Run id:** `{run_id}`\n"
        f"- **Reason:** {reason}\n"
        f"- **Waiting for:** {int(waited // 60)} min so far, rechecking every "
        f"{config.GPU_RECHECK_S}s\n"
        f"- **Gives up at:** {config.GPU_MAX_DEFER_S // 3600}h, after which the brief is "
        "published rule-based with no LLM pass\n"
        f"{harvested}\n"
        "This file is replaced by the real brief as soon as the GPU frees up.\n",
        encoding="utf-8")
    store.update_job(job_id, result={"deferred_note": str(path)})
    return path
