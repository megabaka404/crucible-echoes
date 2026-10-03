import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from crucible_echoes.balance_experiment import paired_exact_p, wilson_interval
from crucible_echoes.simulation import run_batch, strategy_from_name
from crucible_echoes.strategy_benchmark import benchmark_lock, compact_report, merge_reports, run_benchmark


class StrategyBenchmarkTests(unittest.TestCase):
    def test_single_writer_lock_releases_after_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "writer.lock"
            with benchmark_lock(path):
                with self.assertRaisesRegex(RuntimeError, "another process"):
                    with benchmark_lock(path):
                        self.fail("second writer acquired lock")
            with benchmark_lock(path):
                pass

    def test_exact_p_and_small_sample_intervals(self):
        self.assertEqual(1.0, paired_exact_p(0, 0))
        self.assertEqual(1.0, paired_exact_p(2, 2))
        self.assertEqual(0.0625, paired_exact_p(5, 0))
        low, high = wilson_interval(0, 5)
        self.assertEqual(0.0, low)
        self.assertGreater(high, 0.4)
        low, high = wilson_interval(5, 5)
        self.assertLess(low, 0.6)
        self.assertEqual(1.0, high)
        self.assertIsNone(wilson_interval(0, 0))
        with self.assertRaises(ValueError):
            paired_exact_p(-1, 0)
        with self.assertRaises(ValueError):
            wilson_interval(6, 5)

    def test_chunk_indices_reproduce_monolithic_games_and_counters(self):
        def batch(n, start=0):
            return run_batch(n, 20261002, 7, strategy=strategy_from_name("heuristic-v2-content"), start_index=start)
        whole = batch(3)
        first, rest = batch(1), batch(2, 1)
        self.assertEqual(whole.games_detail, first.games_detail + rest.games_detail)
        merged = merge_reports([compact_report(first), compact_report(rest)])
        for key in ("wins", "losses", "aborted", "active_choice_total", "pool_size_distribution", "pool_growth_source_counts",
                    "active_action_total", "active_action_games", "active_action_counts", "active_action_item_counts", "bundle_choice_counts",
                    "pool_flow_tracked_games", "pool_flow_unreconciled_games", "pool_flow_totals", "pool_removal_origin_counts"):
            self.assertEqual(whole.summary[key], merged.summary[key])
        for key in ("win_rate", "average_orders_completed", "average_max_pool_size", "pool_over_30_rate"):
            self.assertAlmostEqual(whole.summary[key], merged.summary[key])
        self.assertEqual(whole.summary["order_progression"], merged.summary["order_progression"])
        self.assertEqual(whole.summary["pool_band_choice_stats"], merged.summary["pool_band_choice_stats"])
        self.assertEqual(whole.summary["offer_tracking"], merged.summary["offer_tracking"])
        self.assertEqual(whole.summary["generator_choice_stats"], merged.summary["generator_choice_stats"])
        self.assertEqual(whole.summary["generator_capability_stats"], merged.summary["generator_capability_stats"])
        for category in merged.content:
            original = {r["id"]: r for r in whole.content[category]}
            for row in merged.content[category]:
                for key in ("offer_count", "choice_count", "acquisition_count", "selected_games", "final_owned_games", "removal_count"):
                    self.assertEqual(original[row["id"]][key], row[key])
        with self.assertRaises(ValueError):
            merge_reports([compact_report(first)] * 2)
        with self.assertRaises(ValueError):
            run_batch(1, start_index=-1)

    def test_merging_legacy_and_reroll_exposure_counters_is_rejected(self):
        current = compact_report(run_batch(1, 77, 7))
        legacy = compact_report(run_batch(1, 77, 7, start_index=1))
        legacy["summary"].pop("offer_tracking")
        with self.assertRaisesRegex(ValueError, "Mismatched offer tracking"):
            merge_reports([current, legacy])
        result = merge_reports([legacy])
        self.assertNotIn("offer_tracking", result.summary)

    def test_legacy_missing_active_telemetry_is_not_reported_as_zero(self):
        part = compact_report(run_batch(1, 42, 7))
        for key in ("active_action_total", "active_action_games", "active_action_counts", "active_action_item_counts", "bundle_choice_counts",
                    "pool_flow_tracked_games", "pool_flow_unreconciled_games", "pool_flow_totals", "pool_removal_origin_counts"):
            part["summary"].pop(key)
        merged = merge_reports([part])
        self.assertNotIn("active_action_total", merged.summary)
        self.assertNotIn("bundle_choice_counts", merged.summary)
        self.assertNotIn("pool_flow_totals", merged.summary)

    def test_resume_reuses_finished_chunks_without_replaying(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "paired.json"
            args = dict(output=output, games=2, seed=42, difficulties=[7],
                        baseline="heuristic-v2-content", candidate="heuristic-v2-content-v2", chunk_size=1)
            partial = run_benchmark(**args, max_chunks=1)
            self.assertFalse(partial["complete"])
            self.assertEqual(1, partial["difficulties"]["7"]["games"])
            completed = run_benchmark(**args, resume=True)
            self.assertTrue(completed["complete"])
            self.assertEqual(2, completed["difficulties"]["7"]["games"])
            with patch("crucible_echoes.strategy_benchmark.run_batch", side_effect=AssertionError("replayed")):
                self.assertEqual(completed, run_benchmark(**args, resume=True))
            with self.assertRaises(ValueError):
                run_benchmark(**{**args, "seed": 99}, resume=True)
            with self.assertRaises(ValueError):
                run_benchmark(**args)
            part = output.with_suffix(".chunks") / "d7_000000.json"
            data = json.loads(part.read_text(encoding="utf-8"))
            data["baseline"]["games"][0]["seed"] += 1
            part.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "seed"):
                run_benchmark(**args, resume=True)

    def test_modified_source_refuses_resume_and_unsaved_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = dict(output=Path(tmp) / "paired.json", games=1, seed=42, difficulties=[7],
                        baseline="heuristic-v2", candidate="heuristic-v2", chunk_size=1)
            with patch("crucible_echoes.strategy_benchmark.source_fingerprint", return_value={"source": "a"}):
                run_benchmark(**args, max_chunks=0)
            with patch("crucible_echoes.strategy_benchmark.source_fingerprint", return_value={"source": "b"}):
                with self.assertRaisesRegex(ValueError, "Resume refused"):
                    run_benchmark(**args, resume=True)
            with patch("crucible_echoes.strategy_benchmark.source_fingerprint", side_effect=[{"source": "a"}, {"source": "a"}, {"source": "b"}]):
                with self.assertRaisesRegex(ValueError, "checkpoint not saved"):
                    run_benchmark(**args, resume=True)
            self.assertFalse((args["output"].with_suffix(".chunks") / "d7_000000.json").exists())
