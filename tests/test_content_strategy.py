from copy import deepcopy
import unittest

from crucible_echoes.content_strategy import ContentAwareV2Strategy
from crucible_echoes.engine import GameEngine
from crucible_echoes.model import PendingChoice
from crucible_echoes.simulation import HeuristicV2Strategy, run_batch, strategy_from_name


class ContentStrategyTests(unittest.TestCase):
    def engine(self):
        engine = GameEngine()
        engine.new_game(20260921)
        engine.s.ingredients.clear()
        engine.s.pending.clear()
        return engine

    def test_registration_keeps_v2_class(self):
        self.assertIs(type(strategy_from_name("heuristic-v2")), HeuristicV2Strategy)
        self.assertIsInstance(strategy_from_name("heuristic-v2-content"), ContentAwareV2Strategy)

    def test_profitable_expiry_is_not_blanket_rejected_and_ids_do_not_matter(self):
        engine = self.engine()
        policy = ContentAwareV2Strategy()
        for def_id in ("glass_shard", "synthetic_expiry"):
            row = deepcopy(engine.catalog.ingredients["glass_shard"])
            row["id"] = def_id
            engine.catalog.ingredients[def_id] = row
            choice = PendingChoice("ingredient", [def_id])
            self.assertIsNone(HeuristicV2Strategy().choose(engine, choice))
            self.assertEqual(1, policy.choose(engine, choice))
        self.assertIsNone(policy.choose(engine, PendingChoice("ingredient", ["scrap_iron"])))

    def test_growth_targets_require_actual_build_support(self):
        engine = self.engine()
        policy = ContentAwareV2Strategy()
        engine.s.spins_left = 10
        row = engine.catalog.ingredients["clockmaker"]
        before = policy._long_term_ingredient_value(engine, row)
        for _ in range(4):
            engine.add_ingredient("spring", emit=False)
        after = policy._long_term_ingredient_value(engine, row)
        self.assertGreater(after, before)
        parchment = engine.catalog.ingredients["parchment"]
        self.assertGreater(policy._long_term_ingredient_value(engine, parchment),
                           HeuristicV2Strategy()._long_term_ingredient_value(engine, parchment))

    def test_item_bonus_scales_with_matching_instances_not_rarity(self):
        engine = self.engine()
        policy = ContentAwareV2Strategy()
        before = policy.score(engine, "item", "copper_coil")
        engine.add_ingredient("water", emit=False)
        self.assertEqual(before, policy.score(engine, "item", "copper_coil"))
        engine.add_ingredient("copper", emit=False)
        self.assertGreater(policy.score(engine, "item", "copper_coil"), before)

    def test_scoring_does_not_modify_rng_or_catalog(self):
        engine = self.engine()
        engine.add_ingredient("spring", emit=False)
        before = deepcopy(engine.s.to_dict())
        catalog = deepcopy(engine.catalog.ingredients)
        policy = ContentAwareV2Strategy()
        for _ in range(3):
            policy.choose(engine, PendingChoice("ingredient", ["glass_shard", "clockmaker", "parchment"]))
        self.assertEqual(before, engine.s.to_dict())
        self.assertEqual(catalog, engine.catalog.ingredients)

    def test_batch_is_reproducible(self):
        a = run_batch(2, 20260921, 7, strategy=ContentAwareV2Strategy())
        b = run_batch(2, 20260921, 7, strategy=ContentAwareV2Strategy())
        self.assertEqual(a.to_dict(), b.to_dict())
        self.assertEqual(0, a.summary["aborted"])
