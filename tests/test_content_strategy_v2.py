from copy import deepcopy
import unittest

from crucible_echoes.content_strategy import ContentAwareV2RevisionStrategy, ContentAwareV2Strategy
from crucible_echoes.engine import GameEngine
from crucible_echoes.model import PendingChoice
from crucible_echoes.simulation import HeuristicV2Strategy, run_batch


class ContentStrategyRevisionTests(unittest.TestCase):
    def engine(self, size=0, def_id="water"):
        engine = GameEngine()
        engine.new_game(20261002)
        engine.s.ingredients.clear()
        engine.s.pending.clear()
        for _ in range(size):
            engine.add_ingredient(def_id, emit=False)
        engine.s.tokens["remove"] = 1
        return engine

    def test_old_content_policy_remains_available_and_unchanged(self):
        engine = self.engine(25)
        engine.s.ingredients[0].permanent_bonus = 100
        self.assertEqual(1, ContentAwareV2Strategy().removal_index(engine))
        self.assertEqual(1, HeuristicV2Strategy().removal_index(engine))
        self.assertEqual("heuristic-v2-content", ContentAwareV2Strategy.name)
        self.assertEqual("heuristic-v2-content-v2", ContentAwareV2RevisionStrategy.name)

    def test_removal_keeps_accumulated_permanent_value(self):
        engine = self.engine(25)
        engine.s.ingredients[0].permanent_bonus = 100
        self.assertEqual(2, ContentAwareV2RevisionStrategy().removal_index(engine))

    def test_removal_considers_negative_permanent_value(self):
        engine = self.engine(25)
        engine.s.ingredients[4].permanent_bonus = -2
        self.assertEqual(5, ContentAwareV2RevisionStrategy().removal_index(engine))

    def test_pool_size_does_not_force_deleting_all_high_value_instances(self):
        engine = self.engine(30)
        for instance in engine.s.ingredients:
            instance.permanent_bonus = 100
        self.assertIsNone(ContentAwareV2RevisionStrategy().removal_index(engine))

    def test_stored_gold_is_cash_out_not_retained_board_value(self):
        engine = self.engine(25)
        engine.s.ingredients[4].stored_gold = 40
        policy = ContentAwareV2RevisionStrategy()
        self.assertLess(policy.instance_retention_components(engine, engine.s.ingredients[4])["cash_out"], 0)
        self.assertEqual(5, policy.removal_index(engine))

    def test_periodic_progress_protects_near_trigger_instance(self):
        engine = self.engine()
        first = engine.add_ingredient("parchment", emit=False)
        second = engine.add_ingredient("parchment", emit=False)
        first.counter = 5
        policy = ContentAwareV2RevisionStrategy()
        a = policy.instance_retention_components(engine, first)
        b = policy.instance_retention_components(engine, second)
        self.assertGreater(a["progress"], b["progress"])

    def test_expiry_progress_is_instance_specific(self):
        engine = self.engine()
        first = engine.add_ingredient("glass_shard", emit=False)
        second = engine.add_ingredient("glass_shard", emit=False)
        first.age = 9
        policy = ContentAwareV2RevisionStrategy()
        self.assertGreater(policy.instance_retention_components(engine, first)["progress"],
                           policy.instance_retention_components(engine, second)["progress"])

    def test_spent_once_only_growth_has_no_future_growth_premium(self):
        engine = self.engine()
        paper = engine.add_ingredient("paper", emit=False)
        policy = ContentAwareV2RevisionStrategy()
        self.assertEqual(0, policy.instance_retention_components(engine, paper)["spent_growth"])
        paper.flags["grown"] = True
        self.assertLess(policy.instance_retention_components(engine, paper)["spent_growth"], 0)

    def test_potion_token_and_signed_gold_fields(self):
        engine = self.engine()
        policy = ContentAwareV2RevisionStrategy()
        old = ContentAwareV2Strategy()
        row = engine.catalog.ingredients["reroll_potion"]
        self.assertEqual(0, old._long_term_ingredient_value(engine, row))
        self.assertEqual(3, policy._long_term_ingredient_value(engine, row))
        costly = deepcopy(row)
        costly["potion"] = {"gold": -12, "token": "essence", "amount": 1}
        self.assertAlmostEqual(-4.2, policy._long_term_ingredient_value(engine, costly))
        self.assertAlmostEqual(7.2, old._long_term_ingredient_value(engine, costly))
        costly["potion"]["amount"] = 2
        self.assertAlmostEqual(-1.2, policy._long_term_ingredient_value(engine, costly))

    def test_growth_chance_is_data_driven_and_draw_fraction_aware(self):
        engine = self.engine()
        policy = ContentAwareV2RevisionStrategy()
        row = engine.catalog.ingredients["paper"]
        self.assertGreater(policy._growth_chance_value(engine, row), 0)
        renamed = deepcopy(row)
        renamed["id"] = "synthetic_growth"
        renamed["script"] = "synthetic_growth_script"
        self.assertEqual(policy._growth_chance_value(engine, row), policy._growth_chance_value(engine, renamed))
        value = policy._growth_chance_value(engine, row)
        for _ in range(40):
            engine.add_ingredient("water", emit=False)
        self.assertLess(policy._growth_chance_value(engine, row), value)

    def test_shared_source_tags_do_not_make_unrelated_tools_core(self):
        engine = self.engine(24)
        engine.add_ingredient("focus_lens", emit=False)
        engine.add_ingredient("dropper", emit=False)
        choice = PendingChoice("ingredient", ["flask"])
        self.assertEqual(1, ContentAwareV2Strategy().choose(engine, choice))
        policy = ContentAwareV2RevisionStrategy()
        self.assertFalse(policy._is_exception_candidate(engine, "flask"))
        self.assertIsNone(policy.choose(engine, choice))

    def test_actual_item_support_can_restore_large_pool_candidate(self):
        engine = self.engine(26)
        engine.s.items.append("advanced_miner_lamp")
        engine.s.items.append("copper_coil")
        policy = ContentAwareV2RevisionStrategy()
        self.assertGreaterEqual(policy._archetype_fit(engine, engine.catalog.ingredients["copper"]), 2.5)
        self.assertEqual(1, policy.choose(engine, PendingChoice("ingredient", ["copper"])))

    def test_aura_support_checks_real_targets(self):
        engine = self.engine(26)
        policy = ContentAwareV2RevisionStrategy()
        row = engine.catalog.ingredients["trainer"]
        before = policy._archetype_fit(engine, row)
        for _ in range(4):
            engine.add_ingredient("kitten", emit=False)
        self.assertGreater(policy._archetype_fit(engine, row), before)

    def test_occupancy_cost_is_not_counted_twice_in_successor(self):
        engine = self.engine(26)
        old = ContentAwareV2Strategy().score_components(engine, "ingredient", "stone")
        new = ContentAwareV2RevisionStrategy().score_components(engine, "ingredient", "stone")
        self.assertEqual(old["pool_cost"], old["pool_pressure"])
        self.assertEqual(0, new["pool_pressure"])
        self.assertEqual(old["pool_cost"], new["pool_cost"])

    def test_no_rng_or_persisted_state_mutation_during_decisions(self):
        engine = self.engine(26)
        before = deepcopy(engine.s.to_dict())
        catalog = deepcopy(engine.catalog.ingredients)
        policy = ContentAwareV2RevisionStrategy()
        for _ in range(3):
            policy.choose(engine, PendingChoice("ingredient", ["paper", "wealth_potion", "flask"]))
            policy.removal_index(engine)
        self.assertEqual(before, engine.s.to_dict())
        self.assertEqual(catalog, engine.catalog.ingredients)

    def test_small_batch_is_reproducible(self):
        a = run_batch(2, 20261002, 7, strategy=ContentAwareV2RevisionStrategy())
        b = run_batch(2, 20261002, 7, strategy=ContentAwareV2RevisionStrategy())
        self.assertEqual(a.to_dict(), b.to_dict())
        self.assertEqual(0, a.summary["aborted"])


if __name__ == "__main__":
    unittest.main()
