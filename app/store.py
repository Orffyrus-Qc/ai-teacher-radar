"""SQLite persistence: harvested items, runs, and the job queue.

One connection per call keeps this thread-safe without a pool; the write volume
here is a few hundred rows per day.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            TEXT PRIMARY KEY,      -- sha1 of the canonical url
    url           TEXT NOT NULL,
    title         TEXT NOT NULL,
    summary       TEXT,
    source        TEXT NOT NULL,
    source_kind   TEXT NOT NULL,
    published_at  TEXT,
    first_seen_at TEXT NOT NULL,
    score         REAL NOT NULL DEFAULT 0,
    buckets       TEXT NOT NULL DEFAULT '[]',
    is_teacher    INTEGER NOT NULL DEFAULT 0,
    extra         TEXT NOT NULL DEFAULT '{}',
    llm_note      TEXT,
    run_id        TEXT
);
CREATE INDEX IF NOT EXISTS items_seen  ON items(first_seen_at);
CREATE INDEX IF NOT EXISTS items_score ON items(score);

CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,          -- search | daily | weekly
    slot         TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,
    new_items    INTEGER DEFAULT 0,
    total_items  INTEGER DEFAULT 0,
    output_path  TEXT,
    gpu_deferred INTEGER DEFAULT 0,
    gpu_wait_s   INTEGER DEFAULT 0,
    detail       TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    slot         TEXT,
    origin       TEXT NOT NULL DEFAULT 'manual',   -- n8n | internal | manual
    state        TEXT NOT NULL,   -- queued|deferred_gpu_busy|running|done|failed|cancelled
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    gpu_reason   TEXT,
    deferred_since TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    result       TEXT NOT NULL DEFAULT '{}',
    error        TEXT,
    model        TEXT            -- per-job LLM override; NULL = configured default
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);

CREATE TABLE IF NOT EXISTS downloads (
    id            TEXT PRIMARY KEY,
    item_id       TEXT,
    url           TEXT NOT NULL,
    title         TEXT NOT NULL,
    model_id      TEXT,
    license_id    TEXT,
    status        TEXT NOT NULL,
    reason        TEXT,
    path          TEXT,
    bytes         INTEGER,
    file_name     TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS downloads_status ON downloads(status);
CREATE INDEX IF NOT EXISTS downloads_url ON downloads(url);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conn():
    cx = sqlite3.connect(config.DB_PATH, timeout=30)
    cx.row_factory = sqlite3.Row
    try:
        cx.execute("PRAGMA journal_mode=WAL")
        yield cx
        cx.commit()
    finally:
        cx.close()


def init() -> None:
    with conn() as cx:
        cx.executescript(SCHEMA)
        _add_missing_columns(cx)


def _add_missing_columns(cx) -> None:
    """CREATE TABLE IF NOT EXISTS never alters an existing table, so a live
    database keeps its old shape unless columns are added explicitly."""
    for table, column, decl in (("jobs", "model", "TEXT"),):
        have = {r["name"] for r in cx.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            cx.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# --------------------------------------------------------------------- items
def upsert_items(items: list[dict], run_id: str) -> tuple[int, list[dict]]:
    """Insert new items; return how many were genuinely new and those rows."""
    new = 0
    fresh: list[dict] = []
    with conn() as cx:
        for it in items:
            exists = cx.execute("SELECT 1 FROM items WHERE id=?", (it["id"],)).fetchone()
            if exists:
                # Re-score in place. Keeping the historical maximum would freeze
                # a bad score in forever and make config changes a no-op.
                cx.execute(
                    "UPDATE items SET score=?, buckets=?, is_teacher=?, extra=? WHERE id=?",
                    (float(it.get("score", 0.0)), json.dumps(it.get("buckets", [])),
                     int(bool(it.get("is_teacher"))), json.dumps(it.get("extra", {})),
                     it["id"]))
                continue
            cx.execute(
                """INSERT INTO items (id,url,title,summary,source,source_kind,published_at,
                                      first_seen_at,score,buckets,is_teacher,extra,run_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (it["id"], it["url"], it["title"], it.get("summary", ""), it["source"],
                 it["source_kind"], it.get("published_at"), utcnow(),
                 float(it.get("score", 0.0)), json.dumps(it.get("buckets", [])),
                 int(bool(it.get("is_teacher"))), json.dumps(it.get("extra", {})), run_id),
            )
            new += 1
            fresh.append(it)
    return new, fresh


def set_llm_note(item_id: str, note: str) -> None:
    with conn() as cx:
        cx.execute("UPDATE items SET llm_note=? WHERE id=?", (note, item_id))


def items_for_run(run_id: str) -> list[dict]:
    with conn() as cx:
        rows = cx.execute("SELECT * FROM items WHERE run_id=? ORDER BY score DESC",
                          (run_id,)).fetchall()
    return [_row_to_item(r) for r in rows]


def items_since(hours: int, min_score: float = 0.0) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    with conn() as cx:
        rows = cx.execute(
            "SELECT * FROM items WHERE first_seen_at >= ? AND score >= ? ORDER BY score DESC",
            (cutoff, min_score)).fetchall()
    return [_row_to_item(r) for r in rows]


def items_between(start: datetime, end: datetime, min_score: float = 0.0) -> list[dict]:
    with conn() as cx:
        rows = cx.execute(
            "SELECT * FROM items WHERE first_seen_at >= ? AND first_seen_at < ? AND score >= ? "
            "ORDER BY score DESC",
            (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"), min_score),
        ).fetchall()
    return [_row_to_item(r) for r in rows]


def _row_to_item(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["buckets"] = json.loads(d.get("buckets") or "[]")
    d["extra"] = json.loads(d.get("extra") or "{}")
    d["is_teacher"] = bool(d.get("is_teacher"))
    return d


# ---------------------------------------------------------------------- runs
def start_run(run_id: str, kind: str, slot: str | None) -> None:
    with conn() as cx:
        cx.execute("INSERT OR REPLACE INTO runs (id,kind,slot,started_at,status) VALUES (?,?,?,?,?)",
                   (run_id, kind, slot, utcnow(), "running"))


def finish_run(run_id: str, **fields) -> None:
    fields.setdefault("finished_at", utcnow())
    if "detail" in fields and not isinstance(fields["detail"], str):
        fields["detail"] = json.dumps(fields["detail"])
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn() as cx:
        cx.execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))


def avg_run_seconds(kind: str, limit: int = 10) -> tuple[float | None, int]:
    """Mean *working* seconds for recent completed runs of this kind.

    GPU wait is subtracted: a run that sat six hours behind a ComfyUI render
    says nothing about how long the work itself takes, and leaving it in would
    make every estimate wildly pessimistic.
    """
    with conn() as cx:
        rows = cx.execute(
            "SELECT started_at, finished_at, gpu_wait_s FROM runs "
            "WHERE kind=? AND status='done' AND finished_at IS NOT NULL "
            "ORDER BY started_at DESC LIMIT ?", (kind, limit)).fetchall()

    spans: list[float] = []
    for r in rows:
        try:
            started = datetime.fromisoformat(r["started_at"])
            finished = datetime.fromisoformat(r["finished_at"])
        except (TypeError, ValueError):
            continue
        secs = (finished - started).total_seconds() - float(r["gpu_wait_s"] or 0)
        if secs > 0:
            spans.append(secs)

    if not spans:
        return None, 0
    return sum(spans) / len(spans), len(spans)


def recent_runs(limit: int = 25) -> list[dict]:
    with conn() as cx:
        rows = cx.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------- jobs
def create_job(job_id: str, kind: str, slot: str | None, origin: str,
               model: str | None = None) -> dict:
    with conn() as cx:
        cx.execute(
            "INSERT INTO jobs (id,kind,slot,origin,state,created_at,model) "
            "VALUES (?,?,?,?,?,?,?)",
            (job_id, kind, slot, origin, "queued", utcnow(), model))
    return get_job(job_id)


def update_job(job_id: str, **fields) -> None:
    if "result" in fields and not isinstance(fields["result"], str):
        fields["result"] = json.dumps(fields["result"])
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn() as cx:
        cx.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))


def get_job(job_id: str) -> dict | None:
    with conn() as cx:
        row = cx.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["result"] = json.loads(d.get("result") or "{}")
    return d


def next_job() -> dict | None:
    """Oldest job still waiting to run (queued or GPU-deferred)."""
    with conn() as cx:
        row = cx.execute(
            "SELECT * FROM jobs WHERE state IN ('queued','deferred_gpu_busy') "
            "ORDER BY created_at ASC LIMIT 1").fetchone()
    if not row:
        return None
    d = dict(row)
    d["result"] = json.loads(d.get("result") or "{}")
    return d


def list_jobs(limit: int = 50, states: list[str] | None = None) -> list[dict]:
    q = "SELECT * FROM jobs"
    args: list = []
    if states:
        q += f" WHERE state IN ({','.join('?' * len(states))})"
        args += states
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with conn() as cx:
        rows = cx.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["result"] = json.loads(d.get("result") or "{}")
        out.append(d)
    return out


def find_queued(kind: str, slot: str | None, model: str | None = None) -> dict | None:
    """An identical job already waiting, if any. Kind, slot and model must all
    match: a 12:00 and an 18:00 search are different work, and so are two
    weeklies asked for with different models."""
    with conn() as cx:
        row = cx.execute(
            "SELECT * FROM jobs WHERE kind=? AND state IN ('queued','deferred_gpu_busy') "
            "AND (slot IS ? OR slot = ?) AND (model IS ? OR model = ?) "
            "ORDER BY created_at ASC LIMIT 1",
            (kind, slot, slot, model, model)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["result"] = json.loads(d.get("result") or "{}")
    return d


def count_jobs_by_kind(states: list[str]) -> dict[str, int]:
    """How many jobs are waiting, per kind. Uncapped, unlike list_jobs."""
    with conn() as cx:
        rows = cx.execute(
            f"SELECT kind, COUNT(*) AS n FROM jobs "
            f"WHERE state IN ({','.join('?' * len(states))}) GROUP BY kind",
            states).fetchall()
    return {r["kind"]: r["n"] for r in rows}


def last_job_from(origin: str) -> dict | None:
    with conn() as cx:
        row = cx.execute("SELECT * FROM jobs WHERE origin=? ORDER BY created_at DESC LIMIT 1",
                         (origin,)).fetchone()
    return dict(row) if row else None


def add_download(*, item_id: str | None, url: str, title: str, model_id: str | None,
                 license_id: str | None, status: str, reason: str | None) -> dict:
    from .util import item_id as url_id
    did = url_id(url or title)
    now = utcnow()
    with conn() as cx:
        cx.execute(
            """INSERT OR IGNORE INTO downloads
               (id,item_id,url,title,model_id,license_id,status,reason,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (did, item_id, url, title, model_id, license_id, status, reason, now, now),
        )
    return get_download(did) or {
        "id": did, "url": url, "title": title, "status": status, "reason": reason,
    }


def get_download(download_id: str) -> dict | None:
    with conn() as cx:
        row = cx.execute("SELECT * FROM downloads WHERE id=?", (download_id,)).fetchone()
    return dict(row) if row else None


def get_download_by_url(url: str) -> dict | None:
    if not url:
        return None
    with conn() as cx:
        row = cx.execute("SELECT * FROM downloads WHERE url=?", (url,)).fetchone()
    return dict(row) if row else None


def update_download(download_id: str, **fields) -> None:
    fields["updated_at"] = utcnow()
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn() as cx:
        cx.execute(f"UPDATE downloads SET {cols} WHERE id=?", (*fields.values(), download_id))


def list_downloads(limit: int = 80) -> list[dict]:
    with conn() as cx:
        rows = cx.execute(
            "SELECT * FROM downloads ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def next_download() -> dict | None:
    with conn() as cx:
        row = cx.execute(
            "SELECT * FROM downloads WHERE status='queued' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def requeue_orphan_downloads() -> int:
    with conn() as cx:
        cur = cx.execute(
            "UPDATE downloads SET status='queued', reason='requeued after restart', "
            "updated_at=? WHERE status='downloading'",
            (utcnow(),),
        )
        return cur.rowcount


def reset_orphans() -> int:
    """A container restart can leave a job stuck in 'running'; requeue those."""
    with conn() as cx:
        cur = cx.execute("UPDATE jobs SET state='queued', started_at=NULL "
                         "WHERE state='running'")
        return cur.rowcount
