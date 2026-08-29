"""Environment + YAML configuration, loaded once at import."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
OUT_DIR = Path(os.getenv("OUT_DIR", ROOT / "out"))
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "radar.sqlite3"


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _csv(name: str) -> list[str]:
    return [p.strip() for p in os.getenv(name, "").split(",") if p.strip()]


TZ = os.getenv("TZ", "UTC")

# --- LLM -------------------------------------------------------------------
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3.5:9b")
LLM_SYNTHESIS_MODEL = os.getenv("LLM_SYNTHESIS_MODEL", "gpt-oss:20b")
LLM_TIMEOUT_S = _i("LLM_TIMEOUT_S", 300)
LLM_ENABLED = _b("LLM_ENABLED", True)
LLM_THINK = _b("LLM_THINK", False)
LLM_MAX_ITEMS = _i("LLM_MAX_ITEMS", 10)

# --- GPU gate --------------------------------------------------------------
GPU_GATE_ENABLED = _b("GPU_GATE_ENABLED", True)
GPU_BUSY_UTIL_PCT = _i("GPU_BUSY_UTIL_PCT", 25)
# Must clear the Windows desktop baseline (~3 GiB of dwm/browser/Discord).
GPU_BUSY_VRAM_MIB = _i("GPU_BUSY_VRAM_MIB", 6000)
GPU_GUARD_INDEXES = [int(x) for x in _csv("GPU_GUARD_INDEXES") if x.isdigit()]
COMFYUI_URL = os.getenv("COMFYUI_URL", "").rstrip("/")
OLLAMA_PS_GATE = _b("OLLAMA_PS_GATE", True)
GPU_RECHECK_S = _i("GPU_RECHECK_S", 120)
GPU_MAX_DEFER_S = _i("GPU_MAX_DEFER_S", 21600)
FETCH_WHEN_GPU_BUSY = _b("FETCH_WHEN_GPU_BUSY", True)

# --- scheduling ------------------------------------------------------------
SCHEDULER_MODE = os.getenv("SCHEDULER_MODE", "auto").strip().lower()
SEARCH_SLOTS = _csv("SEARCH_SLOTS") or ["06:00", "12:00", "18:00", "23:00"]
DAILY_SYNTHESIS_AT = os.getenv("DAILY_SYNTHESIS_AT", "23:40")
WEEKLY_SYNTHESIS_DOW = os.getenv("WEEKLY_SYNTHESIS_DOW", "sun")
WEEKLY_SYNTHESIS_AT = os.getenv("WEEKLY_SYNTHESIS_AT", "23:55")
N8N_GRACE_MIN = _i("N8N_GRACE_MIN", 25)

# --- sources ---------------------------------------------------------------
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
LOOKBACK_HOURS = _i("LOOKBACK_HOURS", 30)
MAX_ITEMS_PER_SOURCE = _i("MAX_ITEMS_PER_SOURCE", 60)
SCORE_THRESHOLD = _f("SCORE_THRESHOLD", 3.0)

USER_AGENT = "ai-teacher-radar/1.0 (+local)"


def _load(name: str) -> dict:
    path = CONFIG_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def sources() -> dict:
    return _load("sources.yaml")


def keywords() -> dict:
    return _load("keywords.yaml")


for _d in (OUT_DIR, DATA_DIR, OUT_DIR / "daily", OUT_DIR / "weekly"):
    _d.mkdir(parents=True, exist_ok=True)
