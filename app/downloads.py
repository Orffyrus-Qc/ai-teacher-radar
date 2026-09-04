"""Queue and fetch licensed Hugging Face GGUF files for leak/drop hits.

One download at a time. GPU-free HTTP. Caps file size. Never ollama pull.
"""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from urllib.parse import quote

import httpx

from . import config, leaks, store

log = logging.getLogger("radar.downloads")

_HF_TREE = "https://huggingface.co/api/models/{repo}/tree/main"
_HF_FILE = "https://huggingface.co/{repo}/resolve/main/{path}"
_PREFER = ("q4_k_m", "q4_k_s", "iq4_xs", "q4_0", "q5_k_m", "q3_k_m", "q5_k_s")
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

_worker: threading.Thread | None = None
_stop = threading.Event()
_wake = threading.Event()


def consider(items: list[dict]) -> list[dict]:
    """Inspect new harvest items; queue licensed HF leaks, record refusals."""
    created: list[dict] = []
    for item in items:
        extra = item.setdefault("extra", {})
        verdict = leaks.inspect(item, force_leak=bool(extra.get("leak_watch")))
        extra["leak_watch"] = bool(verdict["is_leak"])
        extra["leak_reason"] = verdict["reason"]
        extra["leak_downloadable"] = verdict["downloadable"]
        if not verdict["is_leak"]:
            continue
        existing = store.get_download_by_url(item.get("url") or "")
        if existing:
            continue
        if verdict["downloadable"] and config.DOWNLOAD_AUTO:
            status, reason = "queued", verdict["reason"]
        else:
            status = "refused"
            reason = verdict["reason"] if not verdict["downloadable"] else (
                "auto-download off — notified only"
            )
        row = store.add_download(
            item_id=item.get("id"),
            url=item.get("url") or "",
            title=item.get("title") or "",
            model_id=verdict.get("model_id"),
            license_id=verdict.get("license_id"),
            status=status,
            reason=reason,
        )
        created.append(row)
        if status == "queued":
            _wake.set()
    return created


def start_worker() -> None:
    global _worker
    if _worker and _worker.is_alive():
        return
    store.requeue_orphan_downloads()
    _stop.clear()
    _worker = threading.Thread(target=_loop, name="radar-downloads", daemon=True)
    _worker.start()


def stop_worker() -> None:
    _stop.set()
    _wake.set()


def _loop() -> None:
    log.info("download worker started (dir=%s auto=%s max_gib=%s hf_auth=%s)",
             config.DOWNLOADS_DIR, config.DOWNLOAD_AUTO, config.DOWNLOAD_MAX_GIB,
             "yes" if config.hf_auth_ready() else "no")
    while not _stop.is_set():
        row = store.next_download()
        if not row:
            _wake.wait(8)
            _wake.clear()
            continue
        try:
            _fetch(row)
        except Exception:  # noqa: BLE001
            log.exception("download failed %s", row.get("id"))
            store.update_download(row["id"], status="failed",
                                  reason="download worker exception")
    log.info("download worker stopped")


def _fetch(row: dict) -> None:
    store.update_download(row["id"], status="downloading", reason="fetching GGUF listing")
    model_id = (row.get("model_id") or _model_id_from_url(row.get("url") or "") or "").strip()
    if not model_id:
        store.update_download(row["id"], status="failed", reason="no Hugging Face model id")
        return
    picked = pick_gguf(model_id)
    if picked is None:
        store.update_download(
            row["id"], status="failed",
            reason=f"no GGUF under {config.DOWNLOAD_MAX_GIB:g} GiB on the repo",
        )
        return
    dest_dir = config.DOWNLOADS_DIR / _safe_id(model_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(picked["path"]).name
    url = _HF_FILE.format(repo=model_id, path=quote(picked["path"], safe="/"))
    log.info("downloading %s -> %s (%s bytes)", url, dest, picked.get("size"))
    store.update_download(row["id"], reason=f"downloading {picked['path']}",
                          file_name=picked["path"], bytes=picked.get("size") or 0)
    with httpx.Client(timeout=None, follow_redirects=True,
                      headers=config.hf_headers()) as client:
        with client.stream("GET", url) as resp:
            if resp.status_code in {401, 403}:
                store.update_download(
                    row["id"], status="failed",
                    reason="Hugging Face denied this repo — accept the gate in the browser while logged in",
                )
                return
            resp.raise_for_status()
            tmp = dest.with_suffix(dest.suffix + ".part")
            written = 0
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes(1024 * 1024):
                    if _stop.is_set():
                        fh.close()
                        tmp.unlink(missing_ok=True)
                        store.update_download(row["id"], status="queued",
                                              reason="interrupted — will retry")
                        return
                    fh.write(chunk)
                    written += len(chunk)
            tmp.replace(dest)
    store.update_download(
        row["id"], status="done", reason="downloaded",
        path=str(dest), bytes=written, file_name=dest.name,
    )
    log.info("download done %s (%s bytes)", dest, written)


def pick_gguf(model_id: str) -> dict | None:
    cap = int(config.DOWNLOAD_MAX_GIB * 1024 ** 3)
    url = _HF_TREE.format(repo=model_id)
    try:
        r = httpx.get(url, params={"recursive": "true"}, timeout=45.0,
                      headers=config.hf_headers(), follow_redirects=True)
        if r.status_code in {401, 403}:
            log.warning("hf tree %s denied (auth=%s)", model_id,
                        "yes" if config.hf_auth_ready() else "no")
            return None
        r.raise_for_status()
        files = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("hf tree %s failed: %s", model_id, exc)
        return None
    if not isinstance(files, list):
        return None
    ggufs = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "")
        if not path.lower().endswith(".gguf"):
            continue
        size = int(entry.get("size") or 0)
        if size <= 0 or size > cap:
            continue
        ggufs.append({"path": path, "size": size})
    if not ggufs:
        return None

    def rank(item: dict) -> tuple:
        name = item["path"].casefold()
        pref = next((i for i, tag in enumerate(_PREFER) if tag in name), 99)
        return (pref, item["size"])

    ggufs.sort(key=rank)
    return ggufs[0]


def _model_id_from_url(url: str) -> str | None:
    marker = "huggingface.co/"
    i = url.casefold().find(marker)
    if i < 0:
        return None
    rest = url[i + len(marker):].strip("/")
    parts = rest.split("/")
    if len(parts) < 2:
        return None
    if parts[0] in {"datasets", "spaces", "papers"}:
        return None
    return f"{parts[0]}/{parts[1]}"


def _safe_id(model_id: str) -> str:
    return _SAFE.sub("_", model_id.replace("/", "__"))[:180]
