"""Source harvesters. Every collector returns list[dict] and swallows its own
errors, so one dead feed can never abort a run."""
from __future__ import annotations

import logging

from . import arxiv, github, hackernews, huggingface, rss

log = logging.getLogger("radar.sources")

COLLECTORS = {
    "rss": rss.collect,
    "arxiv": arxiv.collect,
    "github": github.collect,
    "hackernews": hackernews.collect,
    "huggingface": huggingface.collect,
}


def collect_all(cfg: dict) -> tuple[list[dict], dict]:
    items: list[dict] = []
    report: dict[str, str] = {}
    for name, fn in COLLECTORS.items():
        try:
            got = fn(cfg)
            items.extend(got)
            report[name] = f"{len(got)} items"
        except Exception as exc:  # noqa: BLE001 - a source must never kill the run
            log.warning("source %s failed: %s", name, exc)
            report[name] = f"failed: {type(exc).__name__}: {exc}"
    return items, report
