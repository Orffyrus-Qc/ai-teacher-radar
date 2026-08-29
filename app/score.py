"""Rule-based relevance scoring and near-duplicate collapsing.

Deliberately runs before any LLM call: the GPU is a scarce, contended resource
here, so the cheap filter decides what is even worth a token.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from .util import age_hours

_WORD = re.compile(r"[a-z0-9]+")


def _terms_pattern(terms: list[str]) -> re.Pattern:
    parts = []
    for t in terms:
        t = str(t).strip().lower()
        if not t:
            continue
        esc = re.escape(t).replace(r"\ ", r"[\s\-]+")
        parts.append(rf"(?<![a-z0-9]){esc}(?![a-z0-9])")
    return re.compile("|".join(parts), re.I) if parts else re.compile(r"(?!x)x")


class Scorer:
    def __init__(self, kw: dict):
        self.buckets = {
            name: (float(spec.get("weight", 1.0)), _terms_pattern(spec.get("terms") or []))
            for name, spec in (kw.get("buckets") or {}).items()
        }
        self.exclude = _terms_pattern(kw.get("exclude") or [])
        self.teacher = _terms_pattern(kw.get("teacher_signals") or [])

    def score(self, item: dict) -> dict:
        text = f"{item.get('title', '')} {item.get('summary', '')}"
        if self.exclude.search(text):
            item["score"] = 0.0
            item["buckets"] = []
            item["dropped"] = "excluded"
            return item

        hits, raw = [], 0.0
        for name, (weight, pattern) in self.buckets.items():
            if pattern.search(text):
                hits.append(name)
                raw += weight

        # A model id is a filename, not a sentence: "Qwen2.5-Coder-7B-GRPO-LoRA"
        # matches five buckets while telling us almost nothing. Damp the keyword
        # signal for models and let measured teacher fitness carry the weight.
        if item.get("source_kind") == "model":
            raw *= 0.35

        # A source everybody trusts still has to say something relevant, so the
        # source weight multiplies rather than adds.
        raw *= float(item.get("extra", {}).get("source_weight", 1.0))

        # Fresh beats stale, but never to zero: a 3-day-old paper can matter.
        hrs = age_hours(item.get("published_at"))
        raw *= 1.25 if hrs <= 12 else 1.0 if hrs <= 36 else 0.85 if hrs <= 96 else 0.7

        # Signals of independent interest.
        extra = item.get("extra", {})
        if extra.get("stars", 0) >= 500:
            raw += 1.0
        if extra.get("points", 0) >= 150:
            raw += 1.0
        if extra.get("upvotes", 0) >= 40:
            raw += 1.0
        raw += float(extra.get("teacher_fitness", 0)) * 0.9

        item["score"] = round(raw, 2)
        item["buckets"] = hits
        # Only something big enough to actually teach counts as a teacher; a
        # 1.5B fine-tune is a student, however many buzzwords are in its name.
        params = extra.get("params_b")
        big_enough = params is None or params >= 7
        item["is_teacher"] = big_enough and (
            bool(self.teacher.search(text)) or extra.get("teacher_fitness", 0) >= 6)
        return item


def _norm(title: str) -> set[str]:
    return set(_WORD.findall(title.lower()))


def dedupe(items: list[dict], threshold: float = 0.82) -> list[dict]:
    """Collapse the same story arriving from several feeds; keep the best-scored
    copy and record the others as corroborating sources."""
    items = sorted(items, key=lambda i: i.get("score", 0), reverse=True)
    kept: list[dict] = []
    for item in items:
        title = item.get("title", "")
        tokens = _norm(title)
        match = None
        for k in kept:
            if item["url"] == k["url"]:
                match = k
                break
            kt = _norm(k.get("title", ""))
            if not tokens or not kt:
                continue
            jaccard = len(tokens & kt) / len(tokens | kt)
            if jaccard >= 0.6 or SequenceMatcher(None, title.lower(),
                                                 k["title"].lower()).ratio() >= threshold:
                match = k
                break
        if match:
            also = match.setdefault("extra", {}).setdefault("also_seen", [])
            if item["source"] not in [a["source"] for a in also]:
                also.append({"source": item["source"], "url": item["url"]})
        else:
            kept.append(item)
    return kept
