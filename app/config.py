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
DOWNLOADS_DIR = Path(os.getenv("DOWNLOADS_DIR", DATA_DIR / "downloads"))


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
# Notes and synthesis use gpt-oss:20b when the GPU gate is idle. Never default
# to qwen3.5:9b (Borg's LoRA student) or qwen3.8:27b (live search). GPU busy or
# LLM_ENABLED=false → rule-based briefs.
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-oss:20b")
LLM_SYNTHESIS_MODEL = os.getenv("LLM_SYNTHESIS_MODEL", "gpt-oss:20b")
LLM_TIMEOUT_S = _i("LLM_TIMEOUT_S", 300)
LLM_ENABLED = _b("LLM_ENABLED", True)
LLM_THINK = _b("LLM_THINK", False)
LLM_MAX_ITEMS = _i("LLM_MAX_ITEMS", 10)
_RESERVED_LLM_PREFIXES = ("qwen3.5:9b", "qwen3.8:27b")


def _reserved_llm(name: str) -> bool:
    n = (name or "").strip().casefold()
    return any(n == prefix or n.startswith(f"{prefix}-") for prefix in _RESERVED_LLM_PREFIXES)


def is_reserved_llm(name: str | None) -> bool:
    """True for models Borg owns. The radar must never load these."""
    return bool(name) and _reserved_llm(name)


def safe_llm(name: str | None) -> str | None:
    """An explicit model request, or None if it is one Borg reserves."""
    return None if is_reserved_llm(name) else name


def notes_model() -> str | None:
    """Model for per-item notes. Never the 9B student or 27B search model."""
    if not LLM_ENABLED:
        return None
    candidate = (LLM_MODEL or "").strip()
    if _reserved_llm(candidate):
        candidate = (LLM_SYNTHESIS_MODEL or "").strip()
    if not candidate or _reserved_llm(candidate):
        return None
    return candidate


def own_llm_models() -> set[str]:
    """Ollama tags that count as *our* radar models for the GPU gate.

    The 9B student and 27B search model are foreign: if they are resident,
    radar publishes a rule-based brief instead of fighting a train or chat.
    """
    return {m for m in (notes_model(), LLM_SYNTHESIS_MODEL) if m and not _reserved_llm(m)}

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
# Collapse a repeat submission onto the identical job already waiting.
DEDUPE_QUEUED_JOBS = _b("DEDUPE_QUEUED_JOBS", True)
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
DOWNLOAD_AUTO = _b("RADAR_AUTO_DOWNLOAD_LEAKS", True)
DOWNLOAD_MAX_GIB = _f("RADAR_DOWNLOAD_MAX_GIB", 25)
LEAK_MIN_SOURCES = _i("RADAR_LEAK_MIN_SOURCES", 2)

USER_AGENT = "ai-teacher-radar/1.0 (+local)"


def hf_token_paths() -> list[Path]:
    paths: list[Path] = []
    explicit = os.getenv("HF_TOKEN_FILE", "").strip()
    if explicit:
        paths.append(Path(explicit))
    home = Path.home()
    paths.extend((
        home / ".cache" / "huggingface" / "token",
        home / ".huggingface" / "token",
        Path("/run/hf_token"),
        Path("/root/.cache/huggingface/token"),
    ))
    return paths


def hf_token() -> str:
    """Read-only Hugging Face login. Never log the value."""
    for key in ("HF_TOKEN", "HUGGINGFACE_HUB_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        value = os.getenv(key, "").strip()
        if value:
            return value
    for path in hf_token_paths():
        try:
            if not path.is_file() or path.stat().st_size == 0:
                continue
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text.splitlines()[0].strip()
    return ""


def hf_auth_ready() -> bool:
    return bool(hf_token())


def hf_headers() -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    token = hf_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _load(name: str) -> dict:
    path = CONFIG_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def sources() -> dict:
    return _load("sources.yaml")


def keywords() -> dict:
    return _load("keywords.yaml")


for _d in (OUT_DIR, DATA_DIR, DOWNLOADS_DIR, OUT_DIR / "daily", OUT_DIR / "weekly"):
    _d.mkdir(parents=True, exist_ok=True)
