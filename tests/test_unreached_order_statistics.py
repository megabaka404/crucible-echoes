import unittest

from crucible_echoes.balance_experiment import compare
from crucible_echoes.catalog import Catalog
from crucible_echoes.simulation import BatchAccumulator, DifficultySweepReport, simulate_game
from crucible_echoes.strategy_benchmark import compact_report, comparison_markdown, merge_reports


class UnreachedOrderStatisticTests(unittest.TestCase):
    def report(self, seed=11, index=0):
        catalog = Catalog.load()
        def start(engine):
            engine.s.ingredients.clear()
            engine.add_ingredient("water", emit=False)
            engine.s.gold = 0
            engine.s.spins_left = 1
        record = simulate_game(seed, 15, catalog=catalog, on_start=start)
        record.index = index
        self.assertEqual("lost", record.status)
        accumulator = BatchAccumulator(catalog, 1)
        accumulator.observe_record(record)
        return accumulator.build(base_seed=11, difficulty=15, strategy_name="heuristic-v1")

    def test_unreached_rate_is_null_and_observed_death_remains_measured(self):
        report = self.report()
        self.assertEqual(1.0, report.summary["order_progression"][0]["conditional_death_rate"])
        for row in report.summary["order_progression"][1:]:
            self.assertEqual((0, 0), (row["reached"], row["died"]))
            self.assertIsNone(row["conditional_death_rate"])
        self.assertIn("| 2 | 0 | 0 | — | — |", report.to_markdown())

    def test_chunk_merge_recalculates_null_from_denominator_not_legacy_rate(self):
        parts = [compact_report(self.report(11, 0)), compact_report(self.report(12, 1))]
        # Legacy reports may encode an undefined rate as zero; counts remain
        # authoritative when reformatting/merging explicitly supplied chunks.
        for part in parts:
            for row in part["summary"]["order_progression"][1:]:
                row["conditional_death_rate"] = 0.0
        merged = merge_reports(parts)
        self.assertEqual(1.0, merged.summary["order_progression"][0]["conditional_death_rate"])
        self.assertIsNone(merged.summary["order_progression"][1]["conditional_death_rate"])

    def test_sweep_and_paired_markdown_render_unreached_as_unobserved(self):
        report = self.report()
        sweep = DifficultySweepReport(11, {15: 1}, "heuristic-v1", {15: report}, [], [])
        self.assertIn("| 15 | 2 | 0 | 0 | — | — |", sweep.to_markdown())
        paired = {"complete": True, "config": {"seed": 11, "fun_mode": "none", "baseline": "x", "candidate": "y", "games": 1},
                  "difficulties": {"15": compare(report, report, allow_strategy_difference=True)}}
        self.assertIn("| 2 | 0/0 | 0/0 | —/— | —/— |", comparison_markdown(paired))
