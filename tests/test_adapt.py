"""Borg-ingest adaptations: scoring caps, license hints, sidecar, reserved LLMs."""
from __future__ import annotations

import json
import sys
import tempfile
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

from app.sources import huggingface as hf
from app.util import (
    assign_roles, make_item, output_learning_allowed, role_for,
)


class LicenseAndHostTests(unittest.TestCase):
    def test_output_learning_allowed_known_licenses(self) -> None:
        self.assertIs(output_learning_allowed("apache-2.0"), True)
        self.assertIs(output_learning_allowed("MIT"), True)
        self.assertIs(output_learning_allowed("llama3"), False)
        self.assertIsNone(output_learning_allowed("unknown"))
        self.assertIsNone(output_learning_allowed(None))

    def test_off_list_hosts_are_noise(self) -> None:
        item = make_item(
            url="https://reddit.com/r/LocalLLaMA/foo",
            title="GLM-5.3",
            source="r/LocalLLaMA",
            source_kind="rss",
        )
        item["buckets"] = ["tooling"]
        self.assertEqual(role_for(item), "noise")


class TeacherFitnessTests(unittest.TestCase):
    def test_community_merge_is_capped_and_not_a_teacher(self) -> None:
        mid = "OliviaRossi/Ornith-Qwopus-KAT-Coder-35B-Merged"
        model = {"tags": ["license:apache-2.0", "code"], "likes": 80}
        self.assertTrue(hf._is_community(mid, model))
        self.assertFalse(hf._is_official(mid))
        fit, notes = hf._teacher_fitness(
            model, mid, 35.0, official=False, community=True)
        self.assertLessEqual(fit, 5)
        self.assertTrue(any("capped" in n for n in notes))

    def test_official_org_gets_a_bonus(self) -> None:
        mid = "Qwen/Qwen3.8-27B"
        model = {"tags": ["license:apache-2.0", "code"], "likes": 80}
        self.assertTrue(hf._is_official(mid))
        fit, notes = hf._teacher_fitness(
            model, mid, 27.0, official=True, community=False)
        self.assertGreaterEqual(fit, 8)
        self.assertTrue(any("official" in n for n in notes))
        self.assertLessEqual(fit, 10)


class NotesModelTests(unittest.TestCase):
    def test_student_and_search_tags_remap_or_skip(self) -> None:
        from app import config

        with patch.object(config, "LLM_ENABLED", True), \
             patch.object(config, "LLM_MODEL", "qwen3.5:9b"), \
             patch.object(config, "LLM_SYNTHESIS_MODEL", "gpt-oss:20b"):
            self.assertEqual(config.notes_model(), "gpt-oss:20b")
            self.assertEqual(config.own_llm_models(), {"gpt-oss:20b"})
        with patch.object(config, "LLM_ENABLED", False), \
             patch.object(config, "LLM_MODEL", "gpt-oss:20b"):
            self.assertIsNone(config.notes_model())


class SidecarTests(unittest.TestCase):
    def test_search_brief_writes_json_sidecar(self) -> None:
        from app import render

        item = make_item(
            url="https://huggingface.co/Qwen/Qwen3.8-27B",
            title="HF model: Qwen/Qwen3.8-27B (27B)",
            summary="official/reviewed org; license apache-2.0",
            source="Hugging Face",
            source_kind="model",
            extra={
                "license": "apache-2.0",
                "params_b": 27.0,
                "teacher_fitness": 9,
                "output_learning_allowed": True,
            },
        )
        item["score"] = 8.0
        item["buckets"] = ["coder"]
        item["is_teacher"] = True
        assign_roles([item])
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            with patch.object(render.config, "OUT_DIR", out):
                path = render.render_search(
                    run_id="search-test", slot="12:00", items=[item],
                    report={"huggingface": "1 items"}, gpu_note="idle",
                    tldr="- review Qwen3.8-27B", new_count=1,
                    llm_state="skipped",
                )
            sidecar = path.with_suffix(".json")
            self.assertTrue(sidecar.is_file())
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(payload["kind"], "search")
            row = payload["items"][0]
            self.assertEqual(row["url"], "https://huggingface.co/Qwen/Qwen3.8-27B")
            self.assertEqual(row["license"], "apache-2.0")
            self.assertEqual(row["params_b"], 27.0)
            self.assertEqual(row["teacher_fitness"], 9)
            self.assertEqual(row["buckets"], ["coder"])
            self.assertEqual(row["role"], "teacher")
            self.assertIs(row["output_learning_allowed"], True)
            self.assertTrue(row["trusted_host"])
            self.assertFalse(row["off_list"])


if __name__ == "__main__":
    unittest.main()
