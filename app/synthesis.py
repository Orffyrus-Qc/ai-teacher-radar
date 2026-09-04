"""Daily and weekly synthesis.

The four slot briefs are a log; the synthesis is the thing you actually read.
It answers one question: given everything that appeared, what should change in
the way I build my coder model?
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, llm, render, store

log = logging.getLogger("radar.synthesis")
TZ = ZoneInfo(config.TZ)

SYS = ("You are the research lead for a solo developer distilling local coding "
       "models on a 16 GB + 12 GB consumer GPU rig. You write short, decisive "
       "briefs. You are allowed to say a week was uneventful. Never invent "
       "facts that are not in the supplied items. "
       "CRITICAL: the items are things other people PUBLISHED or RELEASED. They "
       "are not a log of work the developer has done. Never write that they "
       "downloaded, installed, cloned, pulled or ran anything - they have not. "
       "Recommendations belong in the shortlist sections, phrased as advice.")


def _bounds(day: datetime) -> tuple[datetime, datetime]:
    start_local = datetime.combine(day.date(), dtime.min, tzinfo=TZ)
    return start_local.astimezone(timezone.utc), (start_local + timedelta(days=1)).astimezone(timezone.utc)


def _digest_lines(items: list[dict], limit: int = 60) -> str:
    return "\n".join(
        f"- [{i['source']}|{i['source_kind']}|{i['score']:.1f}] {i['title']} :: "
        f"{(i.get('llm_note') or i.get('summary') or '')[:260]}"
        for i in items[:limit]
    )


def _themes(items: list[dict], top: int = 6) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for i in items:
        counter.update(i.get("buckets") or [])
    return counter.most_common(top)


def _teacher_table(items: list[dict], limit: int = 10) -> list[str]:
    models = [i for i in items if i["source_kind"] == "model"
              and not (i.get("extra") or {}).get("community_variant")]
    models.sort(key=lambda i: i["extra"].get("teacher_fitness", 0), reverse=True)
    if not models:
        return ["*No new teacher candidates appeared.*", ""]
    rows = ["| Model | Params | Fitness | Fits this rig | License |",
            "|---|---|---|---|---|"]
    for m in models[:limit]:
        e = m["extra"]
        vram = e.get("vram") or {}
        rows.append(
            f"| [{e.get('model_id', m['title'])}]({m['url']}) "
            f"| {e.get('params_b') or '?'}B "
            f"| {e.get('teacher_fitness', 0)}/10 "
            f"| {render._fits(vram) if vram else 'unknown'} "
            f"| {e.get('license', '?')} |")
    rows.append("")
    return rows


# ------------------------------------------------------------------- daily
def run_daily(run_id: str, *, use_llm: bool, gpu_note: str,
              day: datetime | None = None, model: str | None = None) -> Path:
    day = day or render.local_now()
    start, end = _bounds(day)
    items = store.items_between(start, end, min_score=config.SCORE_THRESHOLD)
    date_s = day.strftime("%Y-%m-%d")

    folder = config.OUT_DIR / "daily" / date_s
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "SYNTHESIS.md"

    briefs = sorted(p.name for p in folder.glob("*-search.md"))
    themes = _themes(items)
    body, llm_state = _llm_block(
        items,
        f"These are all {len(items)} relevant items harvested on {date_s} across four "
        "scheduled searches.\n\n" + _digest_lines(items) + "\n\n"
        "Write the daily synthesis with exactly these sections:\n"
        "### What appeared today\n(3-5 bullets naming what was published or "
        "released, and by whom. Reporting voice, not a to-do log)\n"
        "### For the distillation pipeline\n(what to change in how I generate "
        "training data, pick a teacher, or run SFT/RL - be specific, cite item titles)\n"
        "### Review shortlist\n(concrete artefacts worth reviewing today — never "
        "say download, pull, or install — or 'nothing today')\n"
        "### Skip\n(what looked interesting but is not worth your time, one line each)",
        use_llm, model=model)

    out = [
        render._fm(title=f"Daily synthesis {date_s}", date=date_s, kind="daily-synthesis",
                   items=len(items), run_id=run_id, tags="[ai, synthesis, daily]"),
        "",
        f"# Daily synthesis — {date_s}",
        "",
        f"*{len(items)} relevant items across {len(briefs)} search run(s) · "
        f"LLM: {llm_state} · GPU: {gpu_note}*",
        "",
        "**Themes:** " + (", ".join(f"{render.BUCKET_TITLES.get(k, k)} ({n})"
                                    for k, n in themes) or "none"),
        "",
        body,
        "",
        "## Teacher-model candidates seen today",
        "",
        *_teacher_table(items),
        "## Top items",
        "",
        *[f"- **[{i['title']}]({i['url']})** — `{i['source']}` · {i['score']:.1f}"
          for i in items[:15]],
        "",
        "## Source briefs",
        "",
        *([f"- [{b}]({b})" for b in briefs] or ["*none*"]),
        "",
    ]
    path.write_text("\n".join(out), encoding="utf-8")
    if use_llm:
        llm.unload()
    return path


# ------------------------------------------------------------------ weekly
def run_weekly(run_id: str, *, use_llm: bool, gpu_note: str,
               day: datetime | None = None, model: str | None = None) -> Path:
    day = day or render.local_now()
    end_local = datetime.combine(day.date(), dtime.min, tzinfo=TZ) + timedelta(days=1)
    start_local = end_local - timedelta(days=7)
    items = store.items_between(start_local.astimezone(timezone.utc),
                                end_local.astimezone(timezone.utc),
                                min_score=config.SCORE_THRESHOLD)

    iso_year, iso_week, _ = day.isocalendar()
    label = f"{iso_year}-W{iso_week:02d}"
    path = config.OUT_DIR / "weekly" / f"{label}.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    themes = _themes(items, top=8)
    days = [(start_local + timedelta(days=n)).strftime("%Y-%m-%d") for n in range(7)]
    dailies = [d for d in days if (config.OUT_DIR / "daily" / d / "SYNTHESIS.md").exists()]

    body, llm_state = _llm_block(
        items,
        f"These are the {len(items)} relevant items from the week "
        f"{start_local:%Y-%m-%d} to {day:%Y-%m-%d}, sorted by score.\n\n"
        + _digest_lines(items, limit=110) + "\n\n"
        "Write the weekly synthesis with exactly these sections:\n"
        "### The week in three sentences\n"
        "### Trends that are real\n(themes with at least two independent items; "
        "name the items. Explicitly call out anything that is hype with one item behind it)\n"
        "### Teacher strategy\n(should I change which model I distil FROM, and why. "
        "Consider that I can run roughly a 30B model at Q4 on 16 GB)\n"
        "### Pipeline changes worth making\n(concrete, ordered, at most 5)\n"
        "### Backlog\n(things to revisit if they get a second data point)",
        use_llm, model=model or config.LLM_SYNTHESIS_MODEL, num_ctx=32768,
        num_predict=4000)

    out = [
        render._fm(title=f"Weekly synthesis {label}", week=label,
                   period=f"{start_local:%Y-%m-%d}..{day:%Y-%m-%d}",
                   kind="weekly-synthesis", items=len(items), run_id=run_id,
                   tags="[ai, synthesis, weekly]"),
        "",
        f"# Weekly synthesis — {label}",
        "",
        f"*{start_local:%Y-%m-%d} → {day:%Y-%m-%d} · {len(items)} relevant items · "
        f"LLM: {llm_state} · GPU: {gpu_note}*",
        "",
        "**Themes:** " + (", ".join(f"{render.BUCKET_TITLES.get(k, k)} ({n})"
                                    for k, n in themes) or "none"),
        "",
        body,
        "",
        "## Teacher-model shortlist (week)",
        "",
        *_teacher_table(items, limit=15),
        "## Highest-signal items of the week",
        "",
        *[f"{n}. **[{i['title']}]({i['url']})** — `{i['source']}` · {i['score']:.1f}"
          for n, i in enumerate(items[:25], 1)],
        "",
        "## Daily syntheses covered",
        "",
        *([f"- [{d}](../daily/{d}/SYNTHESIS.md)" for d in dailies] or ["*none*"]),
        "",
    ]
    path.write_text("\n".join(out), encoding="utf-8")
    if use_llm:
        llm.unload()
    return path


def _llm_block(items: list[dict], prompt: str, use_llm: bool,
               model: str | None = None, num_ctx: int = 24576,
               num_predict: int = 2500) -> tuple[str, str]:
    if not items:
        return "*No items above threshold in this period.*", "n/a"
    if not use_llm:
        return _fallback(items), "skipped (GPU busy or disabled)"
    ok, why = llm.available()
    if not ok:
        return _fallback(items), f"unavailable ({why})"
    model = model or config.LLM_SYNTHESIS_MODEL
    text = llm.generate(prompt, model=model, system=SYS, num_ctx=num_ctx,
                        temperature=0.35, num_predict=num_predict)
    if not text:
        return _fallback(items), f"failed ({model})"
    return text, model


def _fallback(items: list[dict]) -> str:
    lines = ["### What appeared today", ""]
    for i in items[:8]:
        lines.append(f"- **{i['title']}** — `{i['source']}`, score {i['score']:.1f}")
    lines += ["", "### For the distillation pipeline", "",
              "*Rule-based digest only — no LLM pass ran for this synthesis. "
              "Re-run it once the GPU frees up with:*",
              "", "```bash",
              "curl -s -X POST http://127.0.0.1:8791/jobs -H 'content-type: application/json' "
              "-d '{\"kind\":\"daily\"}'", "```", ""]
    return "\n".join(lines)
