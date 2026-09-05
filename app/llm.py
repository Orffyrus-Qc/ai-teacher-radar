"""Thin Ollama client. Every call is optional: if the daemon is down, the model
is missing, or LLM_ENABLED is false, callers get None and fall back to the
rule-based text. A brief must always be produced."""
from __future__ import annotations

import logging

import httpx

from . import config

log = logging.getLogger("radar.llm")


def available(model: str | None = None, *, timeout: float = 8.0) -> tuple[bool, str]:
    if not config.LLM_ENABLED:
        return False, "LLM_ENABLED=false"
    needed = [model] if model else [m for m in (config.notes_model(), config.LLM_SYNTHESIS_MODEL) if m]
    if not needed:
        return False, "no LLM model configured (student/search tags are reserved)"
    try:
        r = httpx.get(f"{config.OLLAMA_BASE_URL}/api/tags", timeout=timeout,
                      headers={"User-Agent": config.USER_AGENT})
        r.raise_for_status()
        tags = {m.get("name") for m in r.json().get("models") or []}
    except Exception as exc:  # noqa: BLE001
        return False, f"ollama unreachable: {type(exc).__name__}"
    missing = [m for m in needed if m not in tags]
    if missing:
        return False, f"model(s) not pulled: {', '.join(missing)}"
    return True, "ok"


def list_models() -> list[dict]:
    """Models Ollama currently has, newest first. Empty list if it is down."""
    try:
        r = httpx.get(f"{config.OLLAMA_BASE_URL}/api/tags", timeout=8.0,
                      headers={"User-Agent": config.USER_AGENT})
        r.raise_for_status()
        models = r.json().get("models") or []
    except Exception as exc:  # noqa: BLE001 - the picker degrades to defaults
        log.warning("could not list Ollama models: %s", exc)
        return []

    out = []
    for m in models:
        name = m.get("name") or m.get("model")
        if not name:
            continue
        details = m.get("details") or {}
        out.append({
            "name": name,
            "size_gb": round((m.get("size") or 0) / 1e9, 1),
            "parameters": details.get("parameter_size"),
            "quantization": details.get("quantization_level"),
        })
    out.sort(key=lambda x: x["name"])
    return out


def generate(prompt: str, *, model: str | None = None, system: str | None = None,
             num_ctx: int = 8192, temperature: float = 0.3,
             num_predict: int = 512) -> str | None:
    model = model or config.notes_model() or config.LLM_SYNTHESIS_MODEL
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        # Hybrid-reasoning models (the qwen3 family) will happily spend a minute
        # thinking about a two-sentence news blurb. This is a summarisation job
        # on a contended GPU, so thinking is off and the output is capped.
        "think": config.LLM_THINK,
        "options": {"temperature": temperature, "num_ctx": num_ctx,
                    "num_predict": num_predict},
    }
    if system:
        payload["system"] = system
    try:
        r = httpx.post(f"{config.OLLAMA_BASE_URL}/api/generate", json=payload,
                       timeout=config.LLM_TIMEOUT_S,
                       headers={"User-Agent": config.USER_AGENT})
        r.raise_for_status()
        data = r.json()
        text = (data.get("response") or "").strip()

        # gpt-oss and friends ignore think=false: they still emit into
        # `thinking`, so a num_predict cap can be consumed entirely by reasoning
        # and leave `response` empty with done_reason=length. Retry uncapped -
        # LLM_TIMEOUT_S is the real bound.
        if not text and data.get("done_reason") == "length" and num_predict > 0:
            log.info("%s spent its budget thinking; retrying uncapped", model)
            payload["options"]["num_predict"] = -1
            r = httpx.post(f"{config.OLLAMA_BASE_URL}/api/generate", json=payload,
                           timeout=config.LLM_TIMEOUT_S,
                           headers={"User-Agent": config.USER_AGENT})
            r.raise_for_status()
            text = (r.json().get("response") or "").strip()
        elif text and data.get("done_reason") == "length":
            log.warning("%s hit the %d-token cap and was cut off mid-output",
                        model, num_predict)
        return text or None
    except Exception as exc:  # noqa: BLE001
        log.warning("llm generate failed (%s): %s", model, exc)
        return None


def unload(model: str | None = None) -> None:
    """Release VRAM immediately so a queued ComfyUI/training job is not blocked
    waiting on an idle keep-alive window."""
    targets = ({model} if model else config.own_llm_models())
    for m in targets:
        try:
            r = httpx.post(f"{config.OLLAMA_BASE_URL}/api/generate",
                           json={"model": m, "prompt": "", "keep_alive": 0},
                           timeout=30.0)
            r.raise_for_status()
            reason = r.json().get("done_reason")
            if reason != "unload":
                log.warning("unload of %s returned done_reason=%r; VRAM may stay held",
                            m, reason)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not unload %s: %s", m, exc)
