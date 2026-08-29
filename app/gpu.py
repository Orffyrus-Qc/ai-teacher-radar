"""GPU-busy detection.

Three independent probes, OR-ed together. Any one of them reporting "busy" is
enough to defer a job, and each probe degrades to "unknown" (not busy) rather
than raising, so a missing nvidia-smi or a stopped ComfyUI never wedges the
queue.

  1. nvidia-smi  - true telemetry, present when the service is started with the
                   NVIDIA device reservation in docker-compose.yml.
  2. ComfyUI     - /queue tells us a diffusion/video job is actually running,
                   which is the honest signal even when VRAM looks calm.
  3. Ollama      - /api/ps lists resident models; a foreign model holding VRAM
                   means someone else is mid-generation.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field, asdict

import httpx

from . import config


@dataclass
class GpuStatus:
    busy: bool = False
    reasons: list[str] = field(default_factory=list)
    gpus: list[dict] = field(default_factory=list)
    probes: dict = field(default_factory=dict)
    #: VRAM held by our own Ollama models. Never counts as "someone else is
    #: using the GPU" - otherwise the radar deadlocks behind its own model.
    own_vram_mib: int = 0

    def as_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "idle"


def _probe_nvidia_smi(st: GpuStatus) -> None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        st.probes["nvidia_smi"] = "unavailable"
        return
    try:
        out = subprocess.run(
            [exe, "--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        st.probes["nvidia_smi"] = f"error: {exc}"
        return

    st.probes["nvidia_smi"] = "ok"
    for line in (l for l in out.splitlines() if l.strip()):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        idx, name, util, used, total = int(parts[0]), parts[1], int(parts[2]), int(parts[3]), int(parts[4])
        st.gpus.append({"index": idx, "name": name, "util_pct": util,
                        "vram_used_mib": used, "vram_total_mib": total,
                        "own_vram_mib": st.own_vram_mib})
        if config.GPU_GUARD_INDEXES and idx not in config.GPU_GUARD_INDEXES:
            continue

        # Discount our own resident models. Ollama keeps a model warm after a
        # run, and counting that as foreign load makes the next run wait for
        # VRAM that only we are holding - a deadlock against ourselves.
        foreign = max(0, used - st.own_vram_mib)
        if util >= config.GPU_BUSY_UTIL_PCT:
            st.busy = True
            st.reasons.append(f"GPU{idx} ({name}) at {util}% utilisation")
        elif foreign >= config.GPU_BUSY_VRAM_MIB:
            st.busy = True
            note = (f" ({st.own_vram_mib} MiB of it ours)" if st.own_vram_mib else "")
            st.reasons.append(f"GPU{idx} ({name}) holding {used} MiB VRAM{note}")


def _probe_comfyui(st: GpuStatus) -> None:
    if not config.COMFYUI_URL:
        st.probes["comfyui"] = "disabled"
        return
    try:
        r = httpx.get(f"{config.COMFYUI_URL}/queue", timeout=10.0,
                      headers={"User-Agent": config.USER_AGENT})
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        st.probes["comfyui"] = f"unreachable: {type(exc).__name__}"
        return

    running = len(data.get("queue_running") or [])
    pending = len(data.get("queue_pending") or [])
    st.probes["comfyui"] = f"running={running} pending={pending}"
    if running:
        st.busy = True
        st.reasons.append(f"ComfyUI is running {running} job(s), {pending} queued")


def _probe_ollama(st: GpuStatus) -> None:
    if not config.OLLAMA_PS_GATE:
        st.probes["ollama"] = "disabled"
        return
    ours = {config.LLM_MODEL, config.LLM_SYNTHESIS_MODEL}
    try:
        r = httpx.get(f"{config.OLLAMA_BASE_URL}/api/ps", timeout=6.0,
                      headers={"User-Agent": config.USER_AGENT})
        r.raise_for_status()
        models = r.json().get("models") or []
    except Exception as exc:  # noqa: BLE001
        st.probes["ollama"] = f"unreachable: {type(exc).__name__}"
        return

    names = [m.get("name") or m.get("model") or "?" for m in models]
    st.own_vram_mib = sum(
        int(m.get("size_vram") or 0) // (1024 * 1024)
        for m in models
        if (m.get("name") or m.get("model")) in ours)
    st.probes["ollama"] = (
        f"resident={names} (ours={st.own_vram_mib} MiB)" if names else "idle")
    foreign = [n for n in names if n not in ours]
    if foreign:
        st.busy = True
        st.reasons.append(f"Ollama busy with {', '.join(foreign)}")


def status() -> GpuStatus:
    """Snapshot every probe. Never raises."""
    st = GpuStatus()
    if not config.GPU_GATE_ENABLED:
        st.probes["gate"] = "disabled"
        return st
    _probe_ollama(st)          # first: establishes how much VRAM is ours
    _probe_nvidia_smi(st)
    _probe_comfyui(st)
    return st


def is_busy() -> tuple[bool, str]:
    st = status()
    return st.busy, st.summary()
