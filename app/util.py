from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import urlparse, urlsplit, urlunsplit

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


TRUSTED_HOSTS = (
    "huggingface.co",
    "ollama.com",
    "arxiv.org",
    "github.com",
    "qwenlm.github.io",
)
OFF_LIST_HOSTS = (
    "reddit.com",
    "openai.com",
    "platform.openai.com",
)
_OUTPUT_LEARNING_OK = frozenset({
    "apache-2.0", "mit", "bsd-2-clause", "bsd-3-clause", "bsd", "unlicense",
    "isc", "cc-by-4.0", "cc0-1.0", "cc0",
})
_OUTPUT_LEARNING_NO_PREFIX = ("llama", "gemma", "openrail", "cc-by-nc", "gpl")


def item_host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _host_matches(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def is_trusted_host(host: str) -> bool:
    return bool(host) and _host_matches(host, TRUSTED_HOSTS)


def is_off_list_host(host: str) -> bool:
    return bool(host) and _host_matches(host, OFF_LIST_HOSTS)


def output_learning_allowed(license_id: str | None) -> bool | None:
    """True/False when the license is known; None means review / unknown.

    A hint for Borg ingest, not a grant. Borg still needs a reviewed teacher
    manifest before any output learning.
    """
    if not license_id:
        return None
    key = license_id.strip().casefold().replace(" ", "-")
    if key in {"unknown", "other", "?", ""}:
        return None
    if key in _OUTPUT_LEARNING_OK or key.startswith("apache") or key.startswith("bsd"):
        return True
    if any(key.startswith(prefix) for prefix in _OUTPUT_LEARNING_NO_PREFIX):
        return False
    return None


def role_for(item: dict) -> str:
    """Exclusive bucket: teacher | student | tooling | research | noise."""
    host = item_host(item.get("url") or "")
    extra = item.get("extra") or {}
    if is_off_list_host(host) or extra.get("community_variant"):
        return "noise"
    kind = item.get("source_kind") or ""
    if kind == "model":
        return "teacher" if item.get("is_teacher") else "student"
    if kind == "paper":
        return "research"
    if kind == "repo" or "tooling" in (item.get("buckets") or []):
        return "tooling"
    return "noise"


def assign_roles(items: list[dict]) -> None:
    for item in items:
        item["role"] = role_for(item)


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
