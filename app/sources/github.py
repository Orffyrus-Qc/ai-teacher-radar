from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from .. import config
from ..util import make_item

log = logging.getLogger("radar.sources.github")

API = "https://api.github.com/search/repositories"


def collect(cfg: dict) -> list[dict]:
    conf = cfg.get("github") or {}
    if not conf.get("enabled", True):
        return []

    weight = float(conf.get("weight", 1.3))
    min_stars = int(conf.get("min_stars", 25))
    # Repos *pushed* recently surface active projects, not just brand-new ones.
    since = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()

    headers = {"User-Agent": config.USER_AGENT,
               "Accept": "application/vnd.github+json"}
    if config.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"

    out: list[dict] = []
    seen: set[str] = set()
    with httpx.Client(timeout=30.0, follow_redirects=True, headers=headers) as client:
        for q in conf.get("queries") or []:
            query = f"{q} pushed:>={since} stars:>={min_stars}"
            try:
                r = client.get(API, params={"q": query, "sort": "updated",
                                            "order": "desc", "per_page": 25})
                if r.status_code == 403:
                    log.warning("github rate-limited; set GITHUB_TOKEN to raise the limit")
                    break
                r.raise_for_status()
                repos = r.json().get("items") or []
            except Exception as exc:  # noqa: BLE001
                log.warning("github query %r failed: %s", q, exc)
                continue

            for repo in repos:
                url = repo.get("html_url")
                if not url or url in seen:
                    continue
                seen.add(url)
                stars = repo.get("stargazers_count", 0)
                out.append(make_item(
                    url=url,
                    title=f"{repo.get('full_name')} ({stars}*)",
                    summary=repo.get("description") or "",
                    source="GitHub", source_kind="repo",
                    published_at=repo.get("pushed_at"),
                    extra={"source_weight": weight, "stars": stars,
                           "language": repo.get("language"), "query": q,
                           "created_at": repo.get("created_at"),
                           "topics": repo.get("topics") or []},
                ))
    return out
