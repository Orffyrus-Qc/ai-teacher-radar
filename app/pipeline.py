"""The search pipeline, split into a GPU-free half and a GPU-using half.

harvest()  - pure HTTP: fetch, score, dedupe, persist. Safe to run at any time,
             even while ComfyUI is saturating the GPU.
publish()  - optional LLM enrichment + Markdown rendering. This is the half the
             GPU gate defers.
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import config, llm, render, score, sources, store

log = logging.getLogger("radar.pipeline")


SYS = ("You are a research scout for a solo developer who trains and distills "
       "local code models on two consumer GPUs (16 GB + 12 GB). Be blunt and "
       "concrete. No hype, no filler, no restating the title.")


def harvest(run_id: str) -> dict:
    """Fetch and score everything. No GPU, no LLM."""
    cfg = config.sources()
    kw = config.keywords()

    raw, report = sources.collect_all(cfg)
    log.info("harvested %d raw items", len(raw))

    scorer = score.Scorer(kw)
    scored = [scorer.score(i) for i in raw]
    relevant = [i for i in scored
                if not i.get("dropped") and i["score"] >= config.SCORE_THRESHOLD]
    deduped = score.dedupe(relevant)
    deduped.sort(key=lambda i: i["score"], reverse=True)

    new_count = store.upsert_items(deduped, run_id)
    log.info("kept %d relevant (%d new)", len(deduped), new_count)

    return {"items": deduped, "report": report, "new_count": new_count,
            "raw_count": len(raw)}


def publish(state: dict, *, run_id: str, slot: str, use_llm: bool,
            gpu_note: str) -> Path:
    """Enrich (optionally) and write the Markdown brief."""
    items = state["items"]
    llm_state = "skipped"

    if use_llm:
        ok, why = llm.available()
        if ok:
            _enrich(items)
            llm_state = config.LLM_MODEL
        else:
            llm_state = f"unavailable ({why})"

    tldr = _tldr(items) if use_llm and llm_state == config.LLM_MODEL else _rule_tldr(items)

    if use_llm and llm_state == config.LLM_MODEL:
        # Hand VRAM back the moment we are done; other jobs are waiting.
        llm.unload()

    return render.render_search(
        run_id=run_id, slot=slot, items=items, report=state["report"],
        gpu_note=gpu_note, tldr=tldr, new_count=state["new_count"],
        llm_state=llm_state,
    )


def _enrich(items: list[dict]) -> None:
    """One short 'why this matters to you' line for the top items only."""
    for item in items[:config.LLM_MAX_ITEMS]:
        prompt = (
            "Item:\n"
            f"TITLE: {item['title']}\n"
            f"SOURCE: {item['source']}\n"
            f"TEXT: {item.get('summary', '')[:1200]}\n\n"
            "In at most 2 sentences, say what a developer building a distillation "
            "pipeline for a local coding model should take from this: is it a "
            "technique to copy, a model to download, a tool to install, or noise? "
            "If it is noise, say so in four words."
        )
        note = llm.generate(prompt, system=SYS, num_ctx=4096, num_predict=160)
        if note:
            item["llm_note"] = note
            store.set_llm_note(item["id"], note)


def _tldr(items: list[dict]) -> str:
    if not items:
        return "*Quiet window — nothing above threshold.*"
    lines = [f"- {i['title']} [{i['source']}] :: {i.get('summary', '')[:220]}"
             for i in items[:25]]
    prompt = (
        "Here are today's harvested items about AI model training, distillation, "
        "fine-tuning and coder models:\n\n" + "\n".join(lines) +
        "\n\nWrite 4-7 bullets. Each bullet: one concrete takeaway, and where "
        "relevant an explicit action (download X, try technique Y, ignore Z). "
        "Rank by usefulness for distilling a better local coding model. "
        "Markdown bullets only, no preamble."
    )
    return llm.generate(prompt, num_ctx=16384, num_predict=700) or _rule_tldr(items)


def _rule_tldr(items: list[dict]) -> str:
    if not items:
        return "*Quiet window — nothing above threshold.*"
    bullets = []
    teachers = [i for i in items if i.get("is_teacher")]
    if teachers:
        bullets.append(f"- {len(teachers)} teacher-relevant item(s); top: "
                       f"**{teachers[0]['title']}**")
    for item in items[:5]:
        bullets.append(f"- **{item['title']}** — {item['source']}, "
                       f"score {item['score']:.1f}")
    bullets.append("- *(rule-based summary; the LLM pass was skipped or unavailable)*")
    return "\n".join(bullets)
