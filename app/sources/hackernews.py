from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from .. import config
from ..util import make_item

log = logging.getLogger("radar.sources.hn")

API = "https://hn.algolia.com/api/v1/search_by_date"


def collect(cfg: dict) -> list[dict]:
    conf = cfg.get("hackernews") or {}
    if not conf.get("enabled", True):
        return []

    weight = float(conf.get("weight", 1.0))
    min_points = int(conf.get("min_points", 30))
    cutoff = int((datetime.now(timezone.utc)
                  - timedelta(hours=config.LOOKBACK_HOURS)).timestamp())

    out: list[dict] = []
    seen: set[str] = set()
    with httpx.Client(timeout=25.0, follow_redirects=True,
                      headers={"User-Agent": config.USER_AGENT}) as client:
        for q in conf.get("queries") or []:
            try:
                r = client.get(API, params={
                    "query": q, "tags": "story",
                    "numericFilters": f"created_at_i>{cutoff},points>={min_points}",
                    "hitsPerPage": 30,
                })
                r.raise_for_status()
                hits = r.json().get("hits") or []
            except Exception as exc:  # noqa: BLE001
                log.warning("hn query %r failed: %s", q, exc)
                continue

            for hit in hits:
                hn_url = f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
                url = hit.get("url") or hn_url
                if url in seen:
                    continue
                seen.add(url)
                out.append(make_item(
                    url=url, title=hit.get("title") or "",
                    summary=(hit.get("story_text") or "")[:600],
                    source="Hacker News", source_kind="discussion",
                    published_at=hit.get("created_at"),
                    extra={"source_weight": weight, "points": hit.get("points", 0),
                           "comments": hit.get("num_comments", 0),
                           "discussion": hn_url, "query": q},
                ))
            time.sleep(0.4)
    return out
