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
    error        TEXT
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
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


# --------------------------------------------------------------------- items
def upsert_items(items: list[dict], run_id: str) -> int:
    """Insert new items; return how many were genuinely new."""
    new = 0
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
    return new


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


def recent_runs(limit: int = 25) -> list[dict]:
    with conn() as cx:
        rows = cx.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------- jobs
def create_job(job_id: str, kind: str, slot: str | None, origin: str) -> dict:
    with conn() as cx:
        cx.execute("INSERT INTO jobs (id,kind,slot,origin,state,created_at) VALUES (?,?,?,?,?,?)",
                   (job_id, kind, slot, origin, "queued", utcnow()))
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


def last_job_from(origin: str) -> dict | None:
    with conn() as cx:
        row = cx.execute("SELECT * FROM jobs WHERE origin=? ORDER BY created_at DESC LIMIT 1",
                         (origin,)).fetchone()
    return dict(row) if row else None


def reset_orphans() -> int:
    """A container restart can leave a job stuck in 'running'; requeue those."""
    with conn() as cx:
        cur = cx.execute("UPDATE jobs SET state='queued', started_at=NULL "
                         "WHERE state='running'")
        return cur.rowcount
