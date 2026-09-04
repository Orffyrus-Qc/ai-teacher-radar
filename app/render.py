"""Markdown writers. Every brief is valid Markdown with YAML front matter, so
the output folder drops straight into Obsidian or any vault."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config
from .util import (
    assign_roles, fits_verdict, is_off_list_host, is_trusted_host, item_host,
    output_learning_allowed,
)

try:
    TZ = ZoneInfo(config.TZ)
except ZoneInfoNotFoundError:
    TZ = timezone.utc

BUCKET_TITLES = {
    "distillation": "Distillation",
    "training_method": "Training method",
    "data": "Training data",
    "model_surgery": "Merging / pruning / rebuilding",
    "efficiency": "Efficiency & quantisation",
    "coder": "Coder models",
    "eval": "Evaluation",
    "tooling": "Tooling",
    "release": "Releases",
    "leak": "Leaked / early-drop",
}


def local_now() -> datetime:
    return datetime.now(TZ)


def _fm(**kv) -> str:
    lines = ["---"]
    for k, v in kv.items():
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(str(x) for x in v)}]")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


def _link(item: dict) -> str:
    return f"[{item['title']}]({item['url']})"


def _meta(item: dict) -> str:
    e = item.get("extra", {})
    bits = [f"`{item['source']}`", f"score {item['score']:.1f}"]
    host = item_host(item.get("url") or "")
    if is_off_list_host(host):
        bits.append(f"off-list `{host}`")
    if e.get("stars"):
        bits.append(f"{e['stars']}★")
    if e.get("points"):
        bits.append(f"{e['points']} pts")
    if e.get("upvotes"):
        bits.append(f"{e['upvotes']} upvotes")
    if item.get("buckets"):
        bits.append(", ".join(BUCKET_TITLES.get(b, b) for b in item["buckets"]))
    also = e.get("also_seen") or []
    if also:
        bits.append(f"+{len(also)} other source(s)")
    return " · ".join(bits)


def _entry(item: dict) -> str:
    out = [f"- **{_link(item)}**", f"  {_meta(item)}"]
    note = item.get("llm_note") or item.get("summary")
    if note:
        out.append(f"  {note[:600].strip()}")
    return "\n".join(out)


def _leak_entry(item: dict) -> str:
    text = _entry(item)
    extra = item.get("extra") or {}
    sources = extra.get("leak_sources") or []
    if sources:
        text += f"\n  leak confirmed by {len(sources)} sources: {', '.join(sources)}"
    return text


def _teacher_entry(item: dict) -> str:
    e = item.get("extra", {})
    lines = [f"- **{_link(item)}** — fitness **{e.get('teacher_fitness', 0)}/10**"]
    facts = []
    if e.get("params_b"):
        facts.append(f"{e['params_b']:g}B params")
    if e.get("license"):
        facts.append(f"license `{e['license']}`")
    allowed = e.get("output_learning_allowed")
    if allowed is True:
        facts.append("training-on-outputs allowed")
    elif allowed is False:
        facts.append("training-on-outputs review")
    if e.get("downloads"):
        facts.append(f"{e['downloads']:,} downloads")
    if facts:
        lines.append(f"  {' · '.join(facts)}")
    vram = e.get("vram") or {}
    if vram:
        lines.append(
            f"  VRAM (weights): fp16 {vram.get('fp16_gib')} GiB · "
            f"Q8 {vram.get('q8_gib')} GiB · Q5 {vram.get('q5_gib')} GiB · "
            f"Q4 {vram.get('q4_gib')} GiB — {_fits(vram)}")
    if e.get("teacher_notes"):
        lines.append(f"  {'; '.join(e['teacher_notes'])}")
    if item.get("llm_note"):
        lines.append(f"  {item['llm_note'][:500]}")
    return "\n".join(lines)


_fits = fits_verdict


def render_search(*, run_id: str, slot: str, items: list[dict], report: dict,
                  gpu_note: str, tldr: str | None, new_count: int,
                  llm_state: str) -> Path:
    now = local_now()
    day = now.strftime("%Y-%m-%d")
    stamp = slot.replace(":", "") if slot else now.strftime("%H%M")
    folder = config.OUT_DIR / "daily" / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stamp}-search.md"

    if not any(i.get("role") for i in items):
        assign_roles(items)
    teachers = [i for i in items if i.get("role") == "teacher"]
    students = [i for i in items if i.get("role") == "student"]
    tools = [i for i in items if i.get("role") == "tooling"]
    papers = [i for i in items if i.get("role") == "research"]
    noise = [i for i in items if i.get("role") == "noise"]

    parts = [
        _fm(title=f"AI Teacher Radar {day} {slot or now.strftime('%H:%M')}",
            date=day, slot=slot or now.strftime("%H:%M"), kind="search",
            run_id=run_id, items=len(items), new_items=new_count,
            tags="[ai, radar, distillation, training]"),
        "",
        f"# AI Teacher Radar — {day} {slot or now.strftime('%H:%M')}",
        "",
        f"*{len(items)} relevant items ({new_count} new since the last run) · "
        f"LLM: {llm_state} · GPU: {gpu_note}*",
        "",
        f"- Teachers: {len(teachers)} · Students: {len(students)} · "
        f"Tooling: {len(tools)} · Noise/off-list: {len(noise)}",
        "",
    ]

    if tldr:
        parts += ["## TL;DR", "", tldr.strip(), ""]

    leaks = [i for i in items if (i.get("extra") or {}).get("leak_watch")]
    parts += _section("Leaked / early-drop models", leaks, _leak_entry,
                      "No model with 2 independent web sources calling it a leak.")
    parts += _section("Teacher-model watch", teachers, _teacher_entry,
                      "Nothing new that would make a better teacher than what you already have.")
    parts += _section("Student-model watch", students, _entry,
                      "No student-sized models above threshold.")
    parts += _section("Training / distillation tooling", tools, _entry,
                      "No new or updated tooling above threshold.")
    parts += _section("Research", papers, _entry, "No new papers above threshold.")
    parts += _section("Noise / off-list", noise, _entry,
                      "Nothing tagged as noise or off-list.")

    parts += ["## Source health", ""]
    for name, state in sorted(report.items()):
        mark = "✓" if "failed" not in state else "✗"
        parts.append(f"- {mark} `{name}` — {state}")
    parts += ["", f"<sub>run `{run_id}` · generated {now.isoformat(timespec='seconds')}</sub>", ""]

    path.write_text("\n".join(parts), encoding="utf-8")
    _write_search_sidecar(
        path, run_id=run_id, day=day, slot=slot or now.strftime("%H:%M"),
        items=items, new_count=new_count, llm_state=llm_state, gpu_note=gpu_note,
    )
    return path


def _write_search_sidecar(md_path: Path, *, run_id: str, day: str, slot: str,
                          items: list[dict], new_count: int, llm_state: str,
                          gpu_note: str) -> Path:
    payload = {
        "schema_version": 1,
        "kind": "search",
        "run_id": run_id,
        "date": day,
        "slot": slot,
        "llm_state": llm_state,
        "gpu_note": gpu_note,
        "new_count": new_count,
        "items": [_sidecar_item(item) for item in items],
    }
    sidecar = md_path.with_suffix(".json")
    sidecar.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
    return sidecar


def _sidecar_item(item: dict) -> dict:
    extra = item.get("extra") or {}
    url = item.get("url") or ""
    host = item_host(url)
    license_id = extra.get("license")
    allowed = extra.get("output_learning_allowed")
    if allowed is None:
        allowed = output_learning_allowed(license_id)
    return {
        "url": url,
        "title": item.get("title"),
        "source": item.get("source"),
        "source_kind": item.get("source_kind"),
        "host": host,
        "trusted_host": is_trusted_host(host),
        "off_list": is_off_list_host(host),
        "role": item.get("role"),
        "score": item.get("score"),
        "buckets": item.get("buckets") or [],
        "license": license_id,
        "params_b": extra.get("params_b"),
        "teacher_fitness": extra.get("teacher_fitness"),
        "output_learning_allowed": allowed,
        "is_teacher": bool(item.get("is_teacher")),
        "community_variant": bool(extra.get("community_variant")),
    }


def _section(title: str, items: list[dict], fmt, empty: str) -> list[str]:
    out = [f"## {title}", ""]
    if not items:
        out += [f"*{empty}*", ""]
        return out
    for item in items:
        out.append(fmt(item))
    out.append("")
    return out
