"""Hugging Face harvester.

Two jobs:
  * surface freshly published / updated *models* and rate each one as a
    candidate TEACHER for a local coder student model, and
  * pull the Daily Papers list.

Teacher fitness is a local-hardware judgement, not a leaderboard: a model is
only useful as a teacher here if you can actually run it long enough to
generate a few hundred thousand reasoning traces.
"""
from __future__ import annotations

import logging
import re
import time

import httpx

from .. import config
from ..util import (
    age_hours, fits_verdict, make_item, output_learning_allowed, parse_date,
    vram_plan,
)

log = logging.getLogger("radar.sources.hf")

MODELS_API = "https://huggingface.co/api/models"
PAPERS_API = "https://huggingface.co/api/daily_papers"

_PARAM_IN_NAME = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])")
# Repackagers: these accounts re-upload other people's weights in another
# format. Useful to download, but never news, and they flood the listing.
_REPACKAGERS = ("mradermacher", "bartowski", "thebloke", "quantfactory",
                "tensorblock", "devquasar", "featherless-ai-quantized",
                "lmstudio-community", "nightmedia", "mlx-community")
_QUANT_SUFFIX = ("-gguf", "-awq", "-gptq", "-exl2", "-mlx", "-i1", "-4bit", "-8bit")
_PERMISSIVE = ("apache-2.0", "mit", "bsd", "openrail", "llama3", "llama4",
               "gemma", "qwen", "cc-by-4.0")
_CODE_HINTS = ("code", "coder", "program", "swe", "dev", "sql")
_REASON_HINTS = ("reason", "think", "r1", "distill", "cot", "math", "o1")
_OFFICIAL_ORGS = frozenset({
    "qwen", "meta-llama", "google", "google-deepmind", "mistralai",
    "deepseek-ai", "microsoft", "ibm-granite", "nvidia", "allenai",
    "bigcode", "tiiuae", "01-ai", "internlm", "moonshotai", "zai-org",
    "thudm", "baai", "huggingface", "facebook", "stabilityai",
    "eleutherai", "openai",
})
_COMMUNITY = re.compile(
    r"(?:^|[-_/\s])(merge|merged|mergekit|abliterat\w*|uncensor\w*|uncen|heretic)(?:$|[-_/\s])",
    re.I,
)


def collect(cfg: dict) -> list[dict]:
    conf = cfg.get("huggingface") or {}
    if not conf.get("enabled", True):
        return []
    out = _models(conf)
    if conf.get("papers", True):
        out.extend(_papers(conf))
    return out


# ------------------------------------------------------------------ models
def _models(conf: dict) -> list[dict]:
    weight = float(conf.get("weight", 1.4))
    limit = int(conf.get("max_models", 120))
    max_params = float(conf.get("max_params_b", 200))
    per_query = max(10, limit // max(1, len(conf.get("model_queries") or [1])))

    out: list[dict] = []
    seen: set[str] = set()
    with httpx.Client(timeout=30.0, follow_redirects=True,
                      headers={"User-Agent": config.USER_AGENT}) as client:
        for q in conf.get("model_queries") or []:
            try:
                r = client.get(MODELS_API, params={
                    "search": q, "sort": "lastModified", "direction": -1,
                    "limit": per_query, "full": "true",
                })
                r.raise_for_status()
                models = r.json()
            except Exception as exc:  # noqa: BLE001
                log.warning("hf model query %r failed: %s", q, exc)
                continue

            for m in models:
                mid = m.get("id") or m.get("modelId")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                modified = parse_date(m.get("lastModified"))
                if modified and age_hours(modified) > config.LOOKBACK_HOURS:
                    continue

                params_b = _params_b(m, mid)
                if params_b and params_b > max_params:
                    continue
                if _is_repackage(mid) and int(m.get("likes") or 0) < 25:
                    continue

                official = _is_official(mid)
                community = _is_community(mid, m)
                fit, notes = _teacher_fitness(
                    m, mid, params_b, official=official, community=community)
                downloads = int(m.get("downloads") or 0)
                likes = int(m.get("likes") or 0)
                # Brand-new repos have no downloads yet; do not punish them.
                if downloads < 50 and likes < 3 and fit < 4:
                    continue
                lic = _license(m)

                out.append(make_item(
                    url=f"https://huggingface.co/{mid}",
                    title=f"HF model: {mid}" + (f" ({params_b:g}B)" if params_b else ""),
                    summary="; ".join(notes),
                    source="Hugging Face", source_kind="model",
                    published_at=modified,
                    extra={
                        "source_weight": weight, "model_id": mid,
                        "params_b": params_b, "likes": likes, "downloads": downloads,
                        "license": lic, "pipeline": m.get("pipeline_tag"),
                        "teacher_fitness": fit, "teacher_notes": notes,
                        "official_org": official,
                        "community_variant": community,
                        "output_learning_allowed": output_learning_allowed(lic),
                        "vram": vram_plan(params_b) if params_b else {},
                        "created_at": parse_date(m.get("createdAt")),
                    },
                ))
            time.sleep(0.3)

    out.sort(key=lambda i: (i["extra"]["teacher_fitness"], i["extra"]["likes"]),
             reverse=True)
    return out[:int(conf.get("keep_models", 25))]


def _is_repackage(mid: str) -> bool:
    """A requant of someone else's weights, not a new model."""
    owner, _, name = mid.lower().partition("/")
    return owner in _REPACKAGERS or name.endswith(_QUANT_SUFFIX)


def _params_b(m: dict, mid: str) -> float | None:
    st = m.get("safetensors") or {}
    total = st.get("total")
    if isinstance(total, (int, float)) and total > 0:
        return round(total / 1e9, 2)
    match = _PARAM_IN_NAME.search(mid.split("/")[-1])
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def _license(m: dict) -> str:
    for tag in m.get("tags") or []:
        if isinstance(tag, str) and tag.startswith("license:"):
            return tag.split(":", 1)[1]
    return "unknown"


def _is_official(mid: str) -> bool:
    owner = mid.split("/", 1)[0].lower()
    return owner in _OFFICIAL_ORGS


def _is_community(mid: str, m: dict) -> bool:
    blob = f"{mid} {' '.join(str(t) for t in (m.get('tags') or []))}"
    return bool(_COMMUNITY.search(blob))


def _teacher_fitness(m: dict, mid: str, params_b: float | None, *,
                     official: bool = False, community: bool = False
                     ) -> tuple[int, list[str]]:
    """0-10. High = worth reviewing as a teacher. Never a pull authorization."""
    text = f"{mid} {' '.join(str(t) for t in (m.get('tags') or []))}".lower()
    score = 0
    notes: list[str] = []

    if any(h in text for h in _CODE_HINTS):
        score += 3
        notes.append("code-domain")
    if any(h in text for h in _REASON_HINTS):
        score += 2
        notes.append("reasoning/distill lineage")
    if _license(m) in _PERMISSIVE or any(p in _license(m) for p in _PERMISSIVE):
        score += 2
        notes.append(f"license {_license(m)}")
    else:
        notes.append(f"license {_license(m)} - check redistribution of outputs")

    if params_b:
        # The size verdict comes from the VRAM plan, never from a hardcoded band,
        # so it cannot contradict the "fits this rig" line rendered next to it.
        verdict = fits_verdict(vram_plan(params_b))
        if 20 <= params_b <= 40:
            score += 3
            notes.append(f"{params_b:g}B - strong teacher/size ratio; {verdict}")
        elif 12 <= params_b < 20:
            score += 2
            notes.append(f"{params_b:g}B - {verdict}")
        elif 40 < params_b <= 80:
            score += 1
            notes.append(f"{params_b:g}B - {verdict}")
        elif params_b > 80:
            notes.append(f"{params_b:g}B - too large to run locally as a teacher")
        else:
            notes.append(f"{params_b:g}B - small; better as a student than a teacher")

    if int(m.get("likes") or 0) >= 50:
        score += 1
        notes.append(f"{m.get('likes')} likes")
    if official:
        score += 2
        notes.append("official/reviewed org")
    if community:
        score = min(score, 5)
        notes.append("community merge/abliteration — fitness capped; not a reviewed teacher")
    return min(score, 10), notes


# ------------------------------------------------------------------ papers
def _papers(conf: dict) -> list[dict]:
    weight = float(conf.get("weight", 1.4))
    try:
        r = httpx.get(PAPERS_API, timeout=25.0,
                      headers={"User-Agent": config.USER_AGENT})
        r.raise_for_status()
        papers = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("hf daily papers failed: %s", exc)
        return []

    out = []
    for entry in papers[:40]:
        paper = entry.get("paper") or {}
        pid = paper.get("id")
        if not pid:
            continue
        out.append(make_item(
            url=f"https://huggingface.co/papers/{pid}",
            title=paper.get("title") or entry.get("title") or "",
            summary=paper.get("summary") or "",
            source="HF Daily Papers", source_kind="paper",
            published_at=entry.get("publishedAt") or paper.get("publishedAt"),
            extra={"source_weight": weight, "upvotes": paper.get("upvotes", 0),
                   "arxiv_id": pid},
        ))
    return out
