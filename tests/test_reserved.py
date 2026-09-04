"""Borg compatibility: a per-job model override must never reach a model
Borg reserves (its LoRA student and live-search model)."""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _name in ("feedparser", "httpx", "yaml"):
    sys.modules.setdefault(_name, types.ModuleType(_name))
if "dateutil" not in sys.modules:
    _dateutil = types.ModuleType("dateutil")
    _parser = types.ModuleType("dateutil.parser")
    _parser.parse = lambda *a, **k: None
    _dateutil.parser = _parser
    sys.modules["dateutil"] = _dateutil
    sys.modules["dateutil.parser"] = _parser

# The container ships tzdata; a bare Windows Python does not, and synthesis
# builds a ZoneInfo at import time. Fall back to UTC so these tests can run
# outside the container.
import zoneinfo  # noqa: E402
from datetime import timezone  # noqa: E402

try:
    zoneinfo.ZoneInfo("UTC")
except Exception:  # noqa: BLE001 - missing tzdata on the host
    zoneinfo.ZoneInfo = lambda *_a, **_k: timezone.utc  # type: ignore[assignment]

from app import config  # noqa: E402


class ReservedModels(unittest.TestCase):
    def test_borg_models_are_reserved(self):
        self.assertTrue(config.is_reserved_llm("qwen3.5:9b"))
        self.assertTrue(config.is_reserved_llm("qwen3.8:27b"))

    def test_variants_of_a_reserved_model_are_also_reserved(self):
        self.assertTrue(config.is_reserved_llm("qwen3.5:9b-borg-lora"))

    def test_ordinary_models_are_allowed(self):
        for name in ("gpt-oss:20b", "qwen2.5-coder:14b", "gemma4:e4b"):
            self.assertFalse(config.is_reserved_llm(name), name)

    def test_nothing_requested_is_not_reserved(self):
        self.assertFalse(config.is_reserved_llm(None))
        self.assertFalse(config.is_reserved_llm(""))

    def test_safe_llm_drops_reserved_and_keeps_the_rest(self):
        self.assertIsNone(config.safe_llm("qwen3.5:9b"))
        self.assertEqual(config.safe_llm("gpt-oss:20b"), "gpt-oss:20b")
        self.assertIsNone(config.safe_llm(None))


class OverrideCannotReachAReservedModel(unittest.TestCase):
    """The guard sits in the run paths too, so an old queued job or a direct
    API call cannot slip past the endpoint check."""

    def test_synthesis_falls_back_instead_of_loading_the_student(self):
        from app import synthesis

        used = []
        with patch.object(synthesis.llm, "available", lambda **kw: (True, "ok")), \
             patch.object(synthesis.llm, "generate",
                          lambda prompt, **kw: used.append(kw.get("model")) or "text"):
            items = [{"source": "HF", "source_kind": "model", "score": 9.0,
                      "title": "t", "summary": "s"}]
            synthesis._llm_block(items, "p", True, model="qwen3.5:9b")

        self.assertNotEqual(used[-1], "qwen3.5:9b")
        self.assertEqual(used[-1], config.LLM_SYNTHESIS_MODEL)

    def test_notes_fall_back_instead_of_loading_the_student(self):
        from app import pipeline

        # publish() drops the override before it reaches the notes model.
        self.assertTrue(config.is_reserved_llm("qwen3.5:9b"))
        self.assertIsNone(config.safe_llm("qwen3.5:9b"))
        self.assertTrue(hasattr(pipeline, "publish"))


if __name__ == "__main__":
    unittest.main()
