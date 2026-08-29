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
from .util import assign_roles

log = logging.getLogger("radar.pipeline")


SYS = ("You are a research scout for a solo developer who trains and distills "
       "local code models on two consumer GPUs (16 GB + 12 GB). Be blunt and "
       "concrete. No hype, no filler, no restating the title. "
       "Actions are review or ignore only. Never say download, pull, ollama pull, "
       "or install weights. Fitness scores are discovery, not a pull grant.")


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
    """Enrich (optionally) and write the Markdown brief plus JSON sidecar."""
    items = state["items"]
    assign_roles(items)
    llm_state = "skipped"
    note_model = config.notes_model() if use_llm else None

    if note_model:
        ok, why = llm.available(model=note_model)
        if ok:
            _enrich(items, model=note_model)
            llm_state = note_model
        else:
            llm_state = f"unavailable ({why})"

    tldr = (_tldr(items, model=note_model)
            if note_model and llm_state == note_model else _rule_tldr(items))

    if note_model and llm_state == note_model:
        # Hand VRAM back the moment we are done; other jobs are waiting.
        llm.unload(note_model)

    return render.render_search(
        run_id=run_id, slot=slot, items=items, report=state["report"],
        gpu_note=gpu_note, tldr=tldr, new_count=state["new_count"],
        llm_state=llm_state,
    )


def _enrich(items: list[dict], *, model: str) -> None:
    """One short 'why this matters to you' line for the top items only."""
    for item in items[:config.LLM_MAX_ITEMS]:
        prompt = (
            "Item:\n"
            f"TITLE: {item['title']}\n"
            f"SOURCE: {item['source']}\n"
            f"ROLE: {item.get('role') or 'unknown'}\n"
            f"TEXT: {item.get('summary', '')[:1200]}\n\n"
            "In at most 2 sentences, say what a developer building a distillation "
            "pipeline for a local coding model should take from this: review a "
            "technique, review a candidate teacher or student, review a tool, "
            "or ignore it as noise. Never say download, pull, or install weights. "
            "If it is noise, say so in four words."
        )
        note = llm.generate(prompt, model=model, system=SYS, num_ctx=4096, num_predict=160)
        if note:
            item["llm_note"] = note
            store.set_llm_note(item["id"], note)


def _group_lines(items: list[dict], role: str, limit: int = 8) -> str:
    rows = [i for i in items if i.get("role") == role][:limit]
    if not rows:
        return "(none)"
    return "\n".join(
        f"- {i['title']} [{i['source']}] :: {i.get('summary', '')[:180]}"
        for i in rows
    )


def _tldr(items: list[dict], *, model: str) -> str:
    if not items:
        return "*Quiet window — nothing above threshold.*"
    prompt = (
        "Items are already classified. Write 4-7 bullets from this inventory.\n\n"
        "TEACHERS:\n" + _group_lines(items, "teacher") + "\n\n"
        "STUDENTS:\n" + _group_lines(items, "student") + "\n\n"
        "TOOLING:\n" + _group_lines(items, "tooling") + "\n\n"
        "NOISE / OFF-LIST:\n" + _group_lines(items, "noise") + "\n\n"
        "Each bullet: one concrete takeaway and an action that is review or ignore. "
        "Never say download, pull, ollama pull, or install. "
        "Rank by usefulness for distilling a better local coding model. "
        "Markdown bullets only, no preamble."
    )
    return llm.generate(prompt, model=model, num_ctx=16384, num_predict=700) or _rule_tldr(items)


def _rule_tldr(items: list[dict]) -> str:
    if not items:
        return "*Quiet window — nothing above threshold.*"
    bullets = []
    for role, label in (
        ("teacher", "teacher"),
        ("student", "student"),
        ("tooling", "tooling"),
        ("noise", "noise/off-list"),
    ):
        got = [i for i in items if i.get("role") == role]
        if got:
            bullets.append(f"- {len(got)} {label} item(s); top: **{got[0]['title']}**")
    bullets.append("- Actions: review or ignore. Fitness scores do not authorize a pull.")
    bullets.append("- *(rule-based summary; the LLM pass was skipped or unavailable)*")
    return "\n".join(bullets)
