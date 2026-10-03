import unittest
from types import SimpleNamespace

from crucible_echoes.balance_experiment import catalog_hash, compare, patched_catalog
from crucible_echoes.catalog import Catalog


class BalanceExperimentTests(unittest.TestCase):
    def test_patch_is_isolated_and_difficulty_cannot_change(self):
        original = Catalog.load()
        before = catalog_hash(original)
        patch = {"collection": "ingredients", "id": "vine", "path": ["growth_after"], "value": 5}
        candidate = patched_catalog(original, [patch])
        self.assertEqual(5, candidate.ingredients["vine"]["growth_after"])
        self.assertEqual(before, catalog_hash(original))
        self.assertNotEqual(before, catalog_hash(candidate))
        with self.assertRaises(ValueError):
            patched_catalog(original, [{**patch, "collection": "progression"}])
        with self.assertRaises(ValueError):
            patched_catalog(original, [{**patch, "value": float("nan")}])
        with self.assertRaises(ValueError):
            patched_catalog(original, [{**patch, "value": 5.5}])

    def test_pairing_counts_discordant_wins_and_rejects_wrong_seeds(self):
        def report(wins):
            return SimpleNamespace(games_detail=[{"seed": i, "won": won} for i, won in enumerate(wins)], summary={}, content={})
        before, after = report([False, False, True]), report([True, False, False])
        result = compare(before, after)
        self.assertEqual((1, 1, 0), (result["gained_wins"], result["lost_wins"], result["win_delta"]))
        self.assertEqual("样本不足", result["evidence"])
        self.assertIsNone(result["approximate_paired_95_interval"])
        after.games_detail[0]["status"] = "aborted"
        with self.assertRaises(ValueError):
            compare(before, after)
        after.games_detail[0]["status"] = "won"
        after.games_detail[0]["seed"] = 99
        with self.assertRaises(ValueError):
            compare(before, after)

    def test_comparison_rejects_different_difficulty_and_duplicate_seeds(self):
        a = SimpleNamespace(difficulty=7, games_detail=[{"seed": 1, "won": False}])
        b = SimpleNamespace(difficulty=8, games_detail=[{"seed": 1, "won": False}])
        with self.assertRaises(ValueError):
            compare(a, b)
        b.difficulty = 7
        a.games_detail *= 2
        b.games_detail *= 2
        with self.assertRaises(ValueError):
            compare(a, b)

    def test_policy_comparison_requires_explicit_opt_in(self):
        a = SimpleNamespace(strategy="heuristic-v2", games_detail=[{"seed": 1, "won": False}], summary={}, content={})
        b = SimpleNamespace(strategy="heuristic-v2-content", games_detail=[{"seed": 1, "won": True}], summary={}, content={})
        with self.assertRaises(ValueError):
            compare(a, b)
        result = compare(a, b, allow_strategy_difference=True)
        self.assertEqual("strategy", result["comparison_kind"])
        self.assertEqual(1, result["gained_wins"])
