from __future__ import annotations

import logging
import time
from urllib.parse import quote_plus

import feedparser
import httpx

from .. import config
from ..util import age_hours, make_item

log = logging.getLogger("radar.sources.arxiv")

API = "http://export.arxiv.org/api/query"


def collect(cfg: dict) -> list[dict]:
    conf = cfg.get("arxiv") or {}
    if not conf.get("enabled", True):
        return []

    weight = float(conf.get("weight", 1.2))
    per_query = max(5, int(conf.get("max_results", 80)) // max(1, len(conf.get("queries") or [1])))
    out: list[dict] = []

    with httpx.Client(timeout=40.0, follow_redirects=True,
                      headers={"User-Agent": config.USER_AGENT}) as client:
        for q in conf.get("queries") or []:
            try:
                out.extend(_query(client, q, per_query, weight))
            except Exception as exc:  # noqa: BLE001
                log.warning("arxiv query %r failed: %s", q, exc)
            time.sleep(3.1)  # arXiv asks for >=3s between API calls
    return out


def _query(client: httpx.Client, q: str, limit: int, weight: float) -> list[dict]:
    terms = " AND ".join(f'all:"{t}"' if " " in t else f"all:{t}" for t in [q])
    url = (f"{API}?search_query={quote_plus(terms)}"
           f"&sortBy=submittedDate&sortOrder=descending&max_results={limit}")
    resp = client.get(url)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)

    items = []
    for entry in parsed.entries:
        published = entry.get("published") or entry.get("updated")
        from ..util import parse_date
        pub = parse_date(published)
        # arXiv announces in batches; allow a slightly wider window than feeds.
        if pub and age_hours(pub) > max(config.LOOKBACK_HOURS, 48):
            continue
        authors = ", ".join(a.get("name", "") for a in (entry.get("authors") or [])[:4])
        items.append(make_item(
            url=entry.get("link", ""), title=entry.get("title", ""),
            summary=entry.get("summary", ""), source="arXiv", source_kind="paper",
            published_at=pub,
            extra={"source_weight": weight, "authors": authors, "query": q},
        ))
    return items
