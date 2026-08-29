"""Markdown writers. Every brief is valid Markdown with YAML front matter, so
the output folder drops straight into Obsidian or any vault."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config
from .util import fits_verdict

TZ = ZoneInfo(config.TZ)

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


def _teacher_entry(item: dict) -> str:
    e = item.get("extra", {})
    lines = [f"- **{_link(item)}** — fitness **{e.get('teacher_fitness', 0)}/10**"]
    facts = []
    if e.get("params_b"):
        facts.append(f"{e['params_b']:g}B params")
    if e.get("license"):
        facts.append(f"license `{e['license']}`")
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

    teachers = [i for i in items if i.get("is_teacher") and i["source_kind"] == "model"]
    tools = [i for i in items if i["source_kind"] == "repo"
             or "tooling" in i.get("buckets", [])]
    papers = [i for i in items if i["source_kind"] == "paper"]
    used = {id(i) for i in teachers + tools + papers}
    rest = [i for i in items if id(i) not in used]

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
    ]

    if tldr:
        parts += ["## TL;DR", "", tldr.strip(), ""]

    parts += _section("Teacher-model watch", teachers, _teacher_entry,
                      "Nothing new that would make a better teacher than what you already have.")
    parts += _section("Training / distillation tooling", tools, _entry,
                      "No new or updated tooling above threshold.")
    parts += _section("Research", papers, _entry, "No new papers above threshold.")
    parts += _section("Other signals", rest, _entry, "Nothing else above threshold.")

    parts += ["## Source health", ""]
    for name, state in sorted(report.items()):
        mark = "✓" if "failed" not in state else "✗"
        parts.append(f"- {mark} `{name}` — {state}")
    parts += ["", f"<sub>run `{run_id}` · generated {now.isoformat(timespec='seconds')}</sub>", ""]

    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def _section(title: str, items: list[dict], fmt, empty: str) -> list[str]:
    out = [f"## {title}", ""]
    if not items:
        out += [f"*{empty}*", ""]
        return out
    for item in items:
        out.append(fmt(item))
    out.append("")
    return out
