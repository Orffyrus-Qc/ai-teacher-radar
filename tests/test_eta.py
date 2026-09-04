"""Average remaining time for the running job."""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
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

from app import config, store, util  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class AverageRunSeconds(unittest.TestCase):
    """store.avg_run_seconds averages only what it should."""

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
    def _run(rid, kind, seconds, *, status="done", wait=0, finished=True):
        start = datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)
        end = start + timedelta(seconds=seconds)
        with store.conn() as cx:
            cx.execute(
                "INSERT INTO runs (id,kind,slot,started_at,finished_at,status,gpu_wait_s) "
                "VALUES (?,?,?,?,?,?,?)",
                (rid, kind, None, _iso(start), _iso(end) if finished else None, status, wait))

    def test_averages_completed_runs(self):
        self._run("a", "search", 120)
        self._run("b", "search", 180)
        avg, samples = store.avg_run_seconds("search")
        self.assertEqual(samples, 2)
        self.assertAlmostEqual(avg, 150.0)

    def test_gpu_wait_is_not_counted_as_work(self):
        # 10 minutes wall clock, 8 of them stuck behind a render.
        self._run("a", "search", 600, wait=480)
        avg, samples = store.avg_run_seconds("search")
        self.assertEqual(samples, 1)
        self.assertAlmostEqual(avg, 120.0)

    def test_ignores_unfinished_and_failed_runs(self):
        self._run("done", "daily", 60)
        self._run("failed", "daily", 999, status="failed")
        self._run("running", "daily", 999, status="running", finished=False)
        avg, samples = store.avg_run_seconds("daily")
        self.assertEqual(samples, 1)
        self.assertAlmostEqual(avg, 60.0)

    def test_other_kinds_are_separate(self):
        self._run("s", "search", 120)
        self.assertEqual(store.avg_run_seconds("weekly"), (None, 0))


class EstimateRemaining(unittest.TestCase):
    """util.estimate_remaining is pure: no database, no worker."""

    NOW = datetime(2026, 9, 4, 8, 5, tzinfo=timezone.utc)

    def setUp(self):
        # The shared stub returns None; this suite needs real ISO parsing.
        self._patch = patch.object(util.dateparser, "parse",
                                   side_effect=datetime.fromisoformat)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def _estimate(self, **kw):
        args = dict(phase="publishing", since=_iso(self.NOW - timedelta(seconds=60)),
                    gpu_wait_s=0.0, avg_total_s=200.0, samples=4,
                    kind="search", now=self.NOW)
        args.update(kw)
        return util.estimate_remaining(**args)

    def test_counts_down_from_the_average(self):
        eta = self._estimate()
        self.assertAlmostEqual(eta["remaining_s"], 140.0)
        self.assertAlmostEqual(eta["elapsed_s"], 60.0)
        self.assertEqual(eta["samples"], 4)
        self.assertIn("average of 4", eta["basis"])

    def test_overrun_clamps_to_zero_rather_than_going_negative(self):
        eta = self._estimate(since=_iso(self.NOW - timedelta(seconds=300)))
        self.assertEqual(eta["remaining_s"], 0.0)

    def test_gpu_wait_is_discounted_from_elapsed(self):
        # 5 minutes since start, but 4 of them were spent deferred.
        eta = self._estimate(since=_iso(self.NOW - timedelta(seconds=300)),
                             gpu_wait_s=240.0)
        self.assertAlmostEqual(eta["elapsed_s"], 60.0)
        self.assertAlmostEqual(eta["remaining_s"], 140.0)

    def test_idle_reports_nothing(self):
        eta = self._estimate(phase="idle")
        self.assertIsNone(eta["remaining_s"])
        self.assertEqual(eta["basis"], "idle")

    def test_deferred_does_not_pretend_to_count_down(self):
        eta = self._estimate(phase="deferred_gpu_busy")
        self.assertIsNone(eta["remaining_s"])
        self.assertIn("GPU", eta["basis"])

    def test_no_history_yet(self):
        eta = self._estimate(avg_total_s=None, samples=0)
        self.assertIsNone(eta["remaining_s"])
        self.assertIn("no completed search runs", eta["basis"])


class HumanDuration(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(util.human_duration(45), "45s")
        self.assertEqual(util.human_duration(130), "2m 10s")
        self.assertEqual(util.human_duration(3900), "1h 05m")
        self.assertEqual(util.human_duration(0), "0s")
        self.assertEqual(util.human_duration(None), "unknown")

    def test_never_shows_negative(self):
        self.assertEqual(util.human_duration(-5), "0s")


if __name__ == "__main__":
    unittest.main()
