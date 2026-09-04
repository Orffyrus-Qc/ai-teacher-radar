"""Detect leaked models from web actuality, then decide if we may download.

A Hugging Face filename is not a leak. At least two independent web sources
must talk about the same model as a leak (RSS, HN, papers — not the model
card). Proprietary dumps (ChatGPT, Claude, Gemini, torrents) stay refused.
"""
from __future__ import annotations

import re
from collections import defaultdict

from . import config
from .util import is_off_list_host, is_trusted_host, item_host, output_learning_allowed

LEAK_TERMS = (
    "leaked", "leaked model", "leaked weights", "weights leak", "model leak",
    "unofficial dump", "unofficial release", "early weights", "weights dropped",
    "weights drop", "pre-release weights", "stolen checkpoint",
)

REFUSE_TERMS = (
    "chatgpt", "gpt-4-leak", "gpt-4o-leak", "gpt-5-leak", "openai leaked",
    "claude-3", "claude-4", "claude-sonnet", "claude-opus", "anthropic leak",
    "gemini leak", "gemini-1.5", "gemini-2.0", "stolen weights", "pirated",
    "magnet:", "torrent",
)

REFUSE_HOSTS = (
    "mega.nz", "mediafire.com", "pixeldrain.com", "gofile.io",
    "anonfiles.com", "t.me", "telegram.me",
)

ACTUALITY_KINDS = frozenset({"rss", "hn", "hackernews", "paper"})
NOT_ACTUALITY_KINDS = frozenset({"model", "repo"})

_HF_REPO = re.compile(r"huggingface\.co/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", re.I)
_MODELISH = re.compile(r"\b[a-z][a-z0-9]+(?:[-._][a-z0-9]+){1,8}\b", re.I)
_STOP = frozenset({
    "leaked", "leak", "leaks", "weights", "weight", "unofficial", "official",
    "release", "released", "model", "models", "dump", "dropped", "early",
    "huggingface", "hugging", "face", "gguf", "safetensors", "checkpoint",
    "stolen", "circulating", "available", "download", "downloads", "blog",
    "reddit", "hacker", "news", "pre-release", "local", "llama",
})


def _blob(item: dict) -> str:
    extra = item.get("extra") or {}
    parts = [
        str(item.get("title") or ""),
        str(item.get("summary") or ""),
        str(item.get("url") or ""),
        str(extra.get("model_id") or ""),
        str(extra.get("license") or ""),
        " ".join(str(b) for b in (item.get("buckets") or [])),
    ]
    return " ".join(parts).casefold()


def talks_about_leak(item: dict) -> bool:
    """Web copy calls it a leak. Repo ids / URLs alone do not count."""
    text = f"{item.get('title') or ''} {item.get('summary') or ''}".casefold()
    return any(term in text for term in LEAK_TERMS)


def is_actuality(item: dict) -> bool:
    """Current web reporting, not the weight listing itself."""
    kind = str(item.get("source_kind") or "").casefold()
    if kind in NOT_ACTUALITY_KINDS:
        return False
    if kind in ACTUALITY_KINDS:
        return True
    host = source_identity(item)
    if host in {"huggingface.co", "hf.co"}:
        return False
    return bool(kind)


def source_identity(item: dict) -> str:
    host = item_host(item.get("url") or "")
    if host:
        if host.endswith("reddit.com"):
            return "reddit.com"
        if host.endswith("huggingface.co") or host == "hf.co":
            return "huggingface.co"
        return host
    return (item.get("source") or "").strip().casefold() or "unknown"


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")


def _usable_key(key: str) -> bool:
    if len(key) < 6:
        return False
    parts = [p for p in key.split("-") if p and p not in _STOP]
    if len(parts) < 2:
        return False
    return any(ch.isdigit() for ch in key) or len(parts) >= 3


def leak_keys(item: dict) -> set[str]:
    keys: set[str] = set()
    extra = item.get("extra") or {}
    mid = str(extra.get("model_id") or "").strip()
    if mid:
        keys.add(_norm(mid))
        if "/" in mid:
            keys.add(_norm(mid.split("/", 1)[1]))
    text = f"{item.get('title') or ''} {item.get('summary') or ''} {item.get('url') or ''}"
    for repo in _HF_REPO.findall(text):
        keys.add(_norm(repo))
        keys.add(_norm(repo.split("/", 1)[1]))
    for match in _MODELISH.findall(text.casefold()):
        if match in _STOP:
            continue
        keys.add(_norm(match))
    return {k for k in keys if _usable_key(k)}


def _pool(harvest: list[dict]) -> list[dict]:
    seen = {item.get("id") for item in harvest if item.get("id")}
    prior: list[dict] = []
    try:
        from . import store
        prior = store.items_since(config.LOOKBACK_HOURS)
    except Exception:  # noqa: BLE001
        prior = []
    return list(harvest) + [row for row in prior if row.get("id") not in seen]


def corroborated_leaks(items: list[dict], *, min_sources: int | None = None) -> dict[str, dict]:
    """Keys that at least *min_sources* independent actuality outlets call a leak."""
    need = int(min_sources or config.LEAK_MIN_SOURCES)
    actuality = [
        item for item in _pool(items)
        if is_actuality(item) and talks_about_leak(item)
    ]
    by_key: dict[str, list[dict]] = defaultdict(list)
    for item in actuality:
        for key in leak_keys(item):
            by_key[key].append(item)
    out: dict[str, dict] = {}
    for key, group in by_key.items():
        sources = sorted({source_identity(item) for item in group if source_identity(item)})
        if len(sources) < need:
            continue
        out[key] = {
            "sources": sources,
            "count": len(sources),
            "urls": [str(item.get("url") or "") for item in group][:8],
        }
    return out


def match_corroboration(item: dict, corroborated: dict[str, dict]) -> dict | None:
    if not corroborated:
        return None
    keys = leak_keys(item)
    hits: list[dict] = []
    for key in keys:
        if key in corroborated:
            hits.append(corroborated[key])
            continue
        for other, meta in corroborated.items():
            if len(key) >= 10 and len(other) >= 10 and (key in other or other in key):
                hits.append(meta)
    if not hits:
        return None
    return max(hits, key=lambda row: int(row.get("count") or 0))


def is_leak(item: dict, corroborated: dict[str, dict] | None = None) -> bool:
    """True only when two+ web sources already called this model a leak."""
    return match_corroboration(item, corroborated or {}) is not None


def refuse_reason(item: dict) -> str | None:
    blob = _blob(item)
    if any(term in blob for term in REFUSE_TERMS):
        return "refused: looks like a proprietary or pirated dump"
    host = item_host(item.get("url") or "")
    if host in REFUSE_HOSTS or any(host.endswith(f".{h}") for h in REFUSE_HOSTS):
        return f"refused: host {host} is not a licensed weight source"
    url = str(item.get("url") or "").casefold()
    if url.startswith("magnet:") or "torrent" in url:
        return "refused: torrent/magnet is not downloaded"
    if is_off_list_host(host):
        return "refused: off-list host (notify only)"
    return None


def inspect(item: dict, corroborated: dict[str, dict] | None = None, *,
            force_leak: bool = False) -> dict:
    """Return leak/download verdict. Never a Borg teacher grant."""
    meta = None if force_leak else match_corroboration(item, corroborated or {})
    leak = force_leak or meta is not None
    extra = item.get("extra") or {}
    license_id = extra.get("license") or extra.get("license_id")
    host = item_host(item.get("url") or "")
    blocked = refuse_reason(item)
    allowed = output_learning_allowed(license_id)
    hf = host == "huggingface.co" or host.endswith(".huggingface.co")
    signed_in = config.hf_auth_ready()
    sources = list((meta or {}).get("sources") or extra.get("leak_sources") or [])
    downloadable = (
        leak
        and blocked is None
        and hf
        and is_trusted_host(host)
        and _account_may_fetch(allowed, license_id, signed_in)
    )
    reason = blocked
    if not leak:
        reason = "not a leak: need 2 independent web sources calling it a leak"
    elif reason is None and not downloadable:
        if not hf:
            reason = "notify only: not a Hugging Face repo"
        elif not signed_in and allowed is not True:
            reason = (
                f"notify only: license {license_id or 'unknown'} needs a "
                "Hugging Face login (huggingface-cli login)"
            )
        else:
            reason = "notify only"
    queued = (
        "Hugging Face leak/drop via your account — queued"
        if signed_in else
        "licensed Hugging Face leak/drop — queued"
    )
    if leak and downloadable:
        reason = f"{queued} ({len(sources)} sources)"
    return {
        "is_leak": leak,
        "downloadable": downloadable,
        "reason": reason,
        "license_id": license_id,
        "model_id": extra.get("model_id"),
        "host": host,
        "hf_auth": signed_in,
        "sources": sources,
        "source_count": len(sources),
    }


def stamp(item: dict, corroborated: dict[str, dict]) -> dict:
    verdict = inspect(item, corroborated)
    extra = item.setdefault("extra", {})
    extra["leak_watch"] = bool(verdict["is_leak"])
    extra["leak_reason"] = verdict["reason"]
    extra["leak_downloadable"] = verdict["downloadable"]
    extra["leak_sources"] = verdict.get("sources") or []
    extra["leak_source_count"] = verdict.get("source_count") or 0
    return verdict


def _account_may_fetch(allowed: bool | None, license_id: str | None, signed_in: bool) -> bool:
    """Permissive licenses always. With a login, gated llama/gemma/unknown may 401 later."""
    if allowed is True:
        return True
    if not signed_in:
        return False
    key = (license_id or "").strip().casefold()
    if key.startswith("openai") or "exclusive" in key:
        return False
    return True
