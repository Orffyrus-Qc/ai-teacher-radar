from __future__ import annotations

import logging
import time

import feedparser
import httpx

from .. import config
from ..util import age_hours, make_item, parse_date

log = logging.getLogger("radar.sources.rss")


def collect(cfg: dict) -> list[dict]:
    out: list[dict] = []
    feeds = cfg.get("rss") or []
    with httpx.Client(timeout=25.0, follow_redirects=True,
                      headers={"User-Agent": config.USER_AGENT}) as client:
        for feed in feeds:
            if feed.get("enabled") is False:
                continue
            try:
                out.extend(_one(client, feed))
            except Exception as exc:  # noqa: BLE001
                log.warning("feed %s failed: %s", feed.get("name"), exc)
            # Reddit 429s when two of its feeds are hit back to back.
            time.sleep(0.6)
    return out


def _one(client: httpx.Client, feed: dict) -> list[dict]:
    resp = client.get(feed["url"])
    if resp.status_code == 429:
        # Reddit in particular throttles hard; one backoff is usually enough.
        time.sleep(8)
        resp = client.get(feed["url"])
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    name = feed.get("name") or feed["url"]
    weight = float(feed.get("weight", 1.0))

    items: list[dict] = []
    for entry in parsed.entries[: config.MAX_ITEMS_PER_SOURCE]:
        link = entry.get("link") or entry.get("id")
        if not link:
            continue
        published = (parse_date(entry.get("published") or entry.get("updated"))
                     or _from_struct(entry))
        # Feeds without dates still pass; we only drop items we know are stale.
        if published and age_hours(published) > config.LOOKBACK_HOURS:
            continue
        summary = entry.get("summary") or ""
        if not summary and entry.get("content"):
            summary = entry["content"][0].get("value", "")
        items.append(make_item(
            url=link, title=entry.get("title", ""), summary=summary,
            source=name, source_kind="rss", published_at=published,
            extra={"source_weight": weight},
        ))
    return items


def _from_struct(entry) -> str | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            import calendar
            return parse_date(calendar.timegm(st))
    return None
