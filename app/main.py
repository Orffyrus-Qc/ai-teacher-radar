"""HTTP surface. n8n drives it; you can drive it too with curl."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from . import config, downloads, gpu, jobs, llm, render, scheduler, store

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s")
log = logging.getLogger("radar")


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init()
    jobs.start_worker()
    downloads.start_worker()
    scheduler.start()
    log.info("ai-teacher-radar ready (tz=%s, slots=%s, mode=%s)",
             config.TZ, config.SEARCH_SLOTS, config.SCHEDULER_MODE)
    yield
    scheduler.shutdown()
    jobs.stop_worker()
    downloads.stop_worker()


api = FastAPI(title="AI Teacher Radar", version="1.0", lifespan=lifespan)


class JobRequest(BaseModel):
    kind: str = "search"
    slot: str | None = None
    #: Override the configured LLM for this job only. None = use the default.
    model: str | None = None


@api.get("/health")
def health():
    ok, why = llm.available()
    return {
        "status": "ok",
        "tz": config.TZ,
        "now": render.local_now().isoformat(timespec="seconds"),
        "scheduler_mode": config.SCHEDULER_MODE,
        "slots": config.SEARCH_SLOTS,
        "llm": {"enabled": config.LLM_ENABLED, "ready": ok, "detail": why,
                "model": config.notes_model() or "(rule-based)",
                "synthesis_model": config.LLM_SYNTHESIS_MODEL},
        "worker": jobs.live(),
        "upcoming": scheduler.upcoming(),
        "hf_auth": {"ready": config.hf_auth_ready()},
    }


@api.get("/gpu")
def gpu_status():
    """Ground truth for the gate. n8n reads this to report a deferral."""
    st = gpu.status()
    return {"busy": st.busy, "summary": st.summary(), "reasons": st.reasons,
            "gpus": st.gpus, "probes": st.probes,
            "policy": {"util_pct": config.GPU_BUSY_UTIL_PCT,
                       "vram_mib": config.GPU_BUSY_VRAM_MIB,
                       "recheck_s": config.GPU_RECHECK_S,
                       "max_defer_s": config.GPU_MAX_DEFER_S,
                       "fetch_when_busy": config.FETCH_WHEN_GPU_BUSY}}


@api.post("/jobs", status_code=202)
def create_job(req: JobRequest, x_radar_origin: str | None = Header(default=None)):
    origin = (x_radar_origin or "manual").strip().lower()
    if origin not in ("n8n", "internal", "manual"):
        origin = "manual"
    try:
        job = jobs.submit(req.kind, slot=req.slot, origin=origin, model=req.model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    st = gpu.status()
    deduped = bool(job.pop("deduplicated", False))
    if deduped:
        note = f"An identical {req.kind} job is already queued; reusing it."
    elif st.busy:
        note = ("GPU is busy — the harvest runs now, the written brief is deferred "
                "until the GPU frees up.")
    else:
        note = "GPU free — running now."
    return {"job": job,
            "deduplicated": deduped,
            "gpu_busy_at_submit": st.busy,
            "gpu": st.summary(),
            "note": note}


@api.get("/jobs")
def list_jobs(limit: int = Query(30, ge=1, le=200), state: str | None = None):
    return {"jobs": store.list_jobs(limit, [state] if state else None),
            "worker": jobs.live()}


@api.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="no such job")
    return job


@api.get("/models")
def models():
    """What the picker offers, and what a job gets if it picks nothing."""
    return {"models": llm.list_models(),
            "defaults": {"notes": config.notes_model(),
                         "synthesis": config.LLM_SYNTHESIS_MODEL}}


@api.get("/queue")
def queue():
    waiting = store.list_jobs(50, ["queued", "deferred_gpu_busy"])
    worker = jobs.live()
    drain = jobs.queue_estimate(worker)
    st = gpu.status()
    # "waiting" is the true backlog; "jobs" is the capped list for display.
    return {"waiting": drain["waiting"], "jobs": waiting, "eta": drain,
            "deferred_by_gpu": [j["id"] for j in waiting if j["state"] == "deferred_gpu_busy"],
            "gpu": {"busy": st.busy, "summary": st.summary()},
            "worker": worker}


@api.get("/runs")
def runs(limit: int = Query(25, ge=1, le=200)):
    return {"runs": store.recent_runs(limit)}


@api.get("/latest")
def latest(kind: str = Query("search", pattern="^(search|daily|weekly)$"),
           fmt: str = Query("md", alias="format", pattern="^(md|json)$")):
    """The most recent Markdown brief, or the search sidecar as JSON."""
    root = config.OUT_DIR / ("weekly" if kind == "weekly" else "daily")
    pattern = "*.md" if kind == "weekly" else (
        "*/SYNTHESIS.md" if kind == "daily" else "*/*-search.md")
    files = sorted(root.glob(pattern))
    if not files:
        raise HTTPException(status_code=404, detail=f"no {kind} output yet")
    path = files[-1]
    if fmt == "json":
        sidecar = path.with_suffix(".json")
        if not sidecar.is_file():
            raise HTTPException(
                status_code=404,
                detail="no JSON sidecar yet; only search briefs emit one",
            )
        try:
            return JSONResponse(json.loads(sidecar.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    return PlainTextResponse(path.read_text(encoding="utf-8"))


@api.get("/downloads")
def list_downloads(limit: int = Query(80, ge=1, le=200)):
    return {
        "downloads": store.list_downloads(limit),
        "folder": str(config.DOWNLOADS_DIR),
        "auto": config.DOWNLOAD_AUTO,
        "max_gib": config.DOWNLOAD_MAX_GIB,
        "hf_auth": config.hf_auth_ready(),
    }


class DownloadRequest(BaseModel):
    url: str
    title: str | None = None
    model_id: str | None = None
    license_id: str | None = None


@api.post("/downloads", status_code=202)
def queue_download(req: DownloadRequest):
    """Manual queue. Still refuses proprietary dumps and off-list hosts."""
    item = {
        "id": None,
        "url": req.url,
        "title": req.title or req.url,
        "summary": "",
        "extra": {
            "model_id": req.model_id,
            "license": req.license_id or "apache-2.0",
        },
        "buckets": ["leak"],
    }
    # Force leak-term so inspect() treats a manual licensed HF queue as a drop.
    if "leaked" not in (req.title or "").casefold() and "leaked" not in req.url.casefold():
        item["title"] = f"leaked model {item['title']}"
    created = downloads.consider([item])
    if not created:
        existing = store.get_download_by_url(req.url)
        if existing:
            return {"download": existing, "note": "already tracked"}
        raise HTTPException(status_code=400, detail="not queued")
    return {"download": created[0]}
