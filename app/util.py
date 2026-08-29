from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from dateutil import parser as dateparser

_TRACKING = re.compile(r"^(utm_|ref_?$|fbclid|gclid|mc_[ce]id|igshid)", re.I)
_WS = re.compile(r"\s+")
_TAGS = re.compile(r"<[^>]+>")


def canonical_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = "&".join(
        q for q in parts.query.split("&")
        if q and not _TRACKING.match(q.split("=", 1)[0])
    )
    netloc = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower() or "https", netloc, path, query, ""))


def item_id(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()


def clean(text: str | None, limit: int = 1200) -> str:
    if not text:
        return ""
    text = _WS.sub(" ", _TAGS.sub(" ", text)).strip()
    return text[:limit]


def parse_date(value) -> str | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        else:
            dt = dateparser.parse(str(value))
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError, TypeError):
        return None


def age_hours(iso: str | None) -> float:
    if not iso:
        return 9999.0
    try:
        dt = dateparser.parse(iso)
    except (ValueError, TypeError):
        return 9999.0
    if dt is None:
        return 9999.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)


def make_item(*, url: str, title: str, summary: str = "", source: str,
              source_kind: str, published_at=None, extra: dict | None = None) -> dict:
    return {
        "id": item_id(url),
        "url": canonical_url(url),
        "title": clean(title, 300) or "(untitled)",
        "summary": clean(summary),
        "source": source,
        "source_kind": source_kind,
        "published_at": parse_date(published_at),
        "extra": extra or {},
    }


def vram_plan(params_b: float) -> dict:
    """Rough weights-only VRAM at common quantisations, plus KV/activation slack."""
    def gib(bits: float) -> float:
        return round(params_b * bits / 8 * 1.15, 1)
    return {"fp16_gib": gib(16), "q8_gib": gib(8), "q5_gib": gib(5), "q4_gib": gib(4)}


def fits_verdict(vram: dict) -> str:
    """16 GiB (RTX 5060 Ti) and 12 GiB (RTX 3060), keeping ~2 GiB for KV cache.

    The single place this judgement is made, so a model can never be described
    as fitting one card in one line and needing two in the next.
    """
    if not vram:
        return "unknown"
    q4, q5, q8 = (vram.get(k) or 999 for k in ("q4_gib", "q5_gib", "q8_gib"))
    if q8 <= 14:
        return "runs at Q8 on the 16 GB card"
    if q5 <= 14:
        return "runs at Q5 on the 16 GB card"
    if q4 <= 14:
        return "runs at Q4 on the 16 GB card"
    if q4 <= 26:
        return "needs both cards (split) at Q4"
    return "too large for this rig - rent or use the API"
