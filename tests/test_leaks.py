from __future__ import annotations

import sys
import types
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

import unittest

from app.leaks import corroborated_leaks, inspect, talks_about_leak
from app.util import make_item


def _news(url: str, title: str, source: str) -> dict:
    return make_item(
        url=url, title=title, source=source, source_kind="rss",
        summary="web actuality calling it a leak",
    )


def _hf(mid: str, license_id: str = "apache-2.0", title: str | None = None) -> dict:
    return make_item(
        url=f"https://huggingface.co/{mid}",
        title=title or f"HF model: {mid}",
        source="Hugging Face",
        source_kind="model",
        extra={"model_id": mid, "license": license_id},
    )


def _map(items: list[dict]) -> dict:
    with patch("app.store.items_since", return_value=[]):
        return corroborated_leaks(items)


class CorroborationTests(unittest.TestCase):
    def test_hf_filename_alone_is_not_a_leak(self) -> None:
        model = _hf("acme/leaked-coder-30b-gguf", title="HF model: acme/leaked-coder-30b-gguf leaked weights")
        cmap = _map([model])
        verdict = inspect(model, cmap)
        self.assertFalse(verdict["is_leak"])
        self.assertFalse(verdict["downloadable"])
        self.assertIn("2 independent", verdict["reason"] or "")

    def test_one_news_source_is_not_enough(self) -> None:
        news = _news(
            "https://simonwillison.net/2026/qwen-leak",
            "Qwen3-Coder-30B leaked weights",
            "Simon Willison",
        )
        model = _hf("Qwen/Qwen3-Coder-30B-A3B")
        cmap = _map([news, model])
        self.assertFalse(inspect(model, cmap)["is_leak"])

    def test_two_hosts_corroborate_matching_hf_model(self) -> None:
        news1 = _news(
            "https://simonwillison.net/2026/qwen-leak",
            "Qwen3-Coder-30B leaked weights",
            "Simon Willison",
        )
        news2 = _news(
            "https://reddit.com/r/LocalLLaMA/qwen-coder-leak",
            "leaked Qwen3-Coder-30B GGUF on Hugging Face",
            "r/LocalLLaMA",
        )
        model = _hf("Qwen/Qwen3-Coder-30B-A3B")
        cmap = _map([news1, news2, model])
        with patch("app.leaks.config.hf_auth_ready", return_value=False):
            verdict = inspect(model, cmap)
        self.assertTrue(verdict["is_leak"])
        self.assertTrue(verdict["downloadable"])
        self.assertGreaterEqual(verdict["source_count"], 2)
        self.assertIn("simonwillison.net", verdict["sources"])
        self.assertIn("reddit.com", verdict["sources"])

    def test_two_reddit_posts_count_as_one_source(self) -> None:
        a = _news(
            "https://reddit.com/r/LocalLLaMA/post1",
            "Qwen3-Coder-30B leaked",
            "r/LocalLLaMA",
        )
        b = _news(
            "https://reddit.com/r/LocalLLaMA/post2",
            "leaked Qwen3-Coder-30B again",
            "r/MachineLearning",
        )
        model = _hf("Qwen/Qwen3-Coder-30B-A3B")
        cmap = _map([a, b, model])
        self.assertFalse(inspect(model, cmap)["is_leak"])

    def test_model_card_does_not_count_as_actuality(self) -> None:
        self.assertTrue(talks_about_leak(_news(
            "https://example.com/p", "foo leaked weights", "Blog",
        )))
        card = _hf("acme/leaked-coder-30b", title="leaked weights acme coder")
        self.assertTrue(talks_about_leak(card))
        cmap = _map([card])
        self.assertEqual(cmap, {})


class LeakInspectTests(unittest.TestCase):
    def test_chatgpt_stays_refused_even_when_corroborated(self) -> None:
        news1 = _news("https://simonwillison.net/chatgpt-leak", "ChatGPT-5-20B leaked weights", "Simon")
        news2 = _news("https://reddit.com/r/LocalLLaMA/gpt", "leaked ChatGPT-5-20B GGUF", "r/LocalLLaMA")
        model = _hf("anon/chatgpt-5-20b-gguf", "mit", title="ChatGPT-5-20B leaked weights GGUF")
        cmap = _map([news1, news2, model])
        with patch("app.leaks.config.hf_auth_ready", return_value=True):
            verdict = inspect(model, cmap)
        self.assertTrue(verdict["is_leak"])
        self.assertFalse(verdict["downloadable"])
        self.assertIn("proprietary", verdict["reason"] or "")

    def test_unknown_license_without_login_is_not_downloaded(self) -> None:
        news1 = _news("https://simonwillison.net/mystery", "mystery-leak-30b leaked weights", "Simon")
        news2 = _news("https://reddit.com/r/LocalLLaMA/mystery", "leaked mystery-leak-30b", "r/LocalLLaMA")
        model = _hf("acme/mystery-leak-30b", "unknown")
        cmap = _map([news1, news2, model])
        with patch("app.leaks.config.hf_auth_ready", return_value=False):
            verdict = inspect(model, cmap)
        self.assertTrue(verdict["is_leak"])
        self.assertFalse(verdict["downloadable"])
        self.assertIn("login", verdict["reason"] or "")

    def test_normal_teacher_is_not_a_leak(self) -> None:
        item = _hf("Qwen/Qwen3.8-27B")
        verdict = inspect(item, {})
        self.assertFalse(verdict["is_leak"])
        self.assertFalse(verdict["downloadable"])


class PickGgufTests(unittest.TestCase):
    def test_prefers_q4_k_m_under_cap(self) -> None:
        from app import downloads

        files = [
            {"path": "model-Q8_0.gguf", "size": 32 * 1024 ** 3},
            {"path": "model-Q4_K_M.gguf", "size": 19 * 1024 ** 3},
            {"path": "model-Q3_K_M.gguf", "size": 12 * 1024 ** 3},
        ]

        class _Resp:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self):
                return files

        with patch.object(downloads, "httpx") as httpx:
            httpx.get.return_value = _Resp()
            with patch.object(downloads.config, "DOWNLOAD_MAX_GIB", 25):
                picked = downloads.pick_gguf("acme/leaked-coder")
        self.assertIsNotNone(picked)
        self.assertEqual(picked["path"], "model-Q4_K_M.gguf")


if __name__ == "__main__":
    unittest.main()
