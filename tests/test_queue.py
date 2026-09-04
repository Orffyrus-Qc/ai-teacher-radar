"""Queue bookkeeping: true backlog counts, and not stacking identical jobs."""
from __future__ import annotations

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

from app import config, store  # noqa: E402


class QueueBookkeeping(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = patch.object(config, "DB_PATH",
                                   Path(self._tmp.name) / "radar.sqlite3")
        self._patch.start()
        store.init()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    @staticmethod
    def _job(jid, kind, *, slot=None, state="queued", model=None):
        store.create_job(jid, kind, slot, "manual", model)
        if state != "queued":
            store.update_job(jid, state=state)

    # ---------------------------------------------------------- counting
    def test_counts_are_not_capped_by_the_display_limit(self):
        for i in range(120):
            self._job(f"w{i}", "weekly")
        # list_jobs caps at its limit; the count must not.
        self.assertEqual(len(store.list_jobs(50, ["queued"])), 50)
        self.assertEqual(store.count_jobs_by_kind(["queued"]), {"weekly": 120})

    def test_counts_group_by_kind_and_ignore_finished_jobs(self):
        self._job("a", "weekly")
        self._job("b", "daily")
        self._job("c", "daily")
        self._job("d", "search", state="done")
        self.assertEqual(store.count_jobs_by_kind(["queued"]), {"weekly": 1, "daily": 2})

    # --------------------------------------------------------- dedup key
    def test_nothing_queued_means_no_duplicate(self):
        self.assertIsNone(store.find_queued("weekly", None))

    def test_finds_an_identical_waiting_job(self):
        self._job("a", "weekly")
        found = store.find_queued("weekly", None)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], "a")

    def test_a_different_slot_is_different_work(self):
        self._job("noon", "search", slot="12:00")
        self.assertIsNone(store.find_queued("search", "18:00"))
        self.assertEqual(store.find_queued("search", "12:00")["id"], "noon")

    def test_a_finished_job_is_not_a_duplicate(self):
        self._job("a", "weekly", state="done")
        self.assertIsNone(store.find_queued("weekly", None))

    def test_a_gpu_deferred_job_still_counts_as_waiting(self):
        self._job("a", "weekly", state="deferred_gpu_busy")
        self.assertEqual(store.find_queued("weekly", None)["id"], "a")

    def test_a_different_model_is_different_work(self):
        self._job("gpt", "weekly", model="gpt-oss:20b")
        # Asking for the same synthesis from another model is a real request.
        self.assertIsNone(store.find_queued("weekly", None, "qwen3.5:9b"))
        self.assertEqual(store.find_queued("weekly", None, "gpt-oss:20b")["id"], "gpt")

    def test_default_model_does_not_match_an_explicit_one(self):
        self._job("explicit", "daily", model="qwen3.5:9b")
        self.assertIsNone(store.find_queued("daily", None, None))

    def test_the_model_is_stored_on_the_job(self):
        self._job("a", "weekly", model="qwen2.5-coder:14b")
        self.assertEqual(store.get_job("a")["model"], "qwen2.5-coder:14b")

    def test_returns_the_oldest_so_repeats_collapse_onto_one(self):
        self._job("first", "weekly")
        self._job("second", "weekly")
        self.assertEqual(store.find_queued("weekly", None)["id"], "first")


if __name__ == "__main__":
    unittest.main()
