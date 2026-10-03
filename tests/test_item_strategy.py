from copy import deepcopy
import unittest

from crucible_echoes.action_strategy import ContentActionStrategy
from crucible_echoes.engine import GameEngine
from crucible_echoes.item_strategy import ItemEconomyStrategy
from crucible_echoes.simulation import run_batch, strategy_from_name


class ItemEconomyStrategyTests(unittest.TestCase):
    def engine(self):
        engine = GameEngine()
        engine.new_game(20261015)
        engine.s.spin = 10
        engine.s.spins_left = 5
        engine.s.stats["event_counts"] = {"removed": 20, "ingredient_added": 30, "order_completed": 2}
        return engine

    def test_recurring_tokens_are_amount_and_horizon_aware(self):
        engine = self.engine()
        policy = ItemEconomyStrategy()
        one = policy.item_economy_components(engine, {"per_spin_token": {"token": "roll", "amount": 1}})
        two = policy.item_economy_components(engine, {"per_spin_token": {"token": "delete", "amount": 2}})
        self.assertEqual(15, one["item_recurring_tokens"])
        self.assertEqual(30, two["item_recurring_tokens"])
        engine.s.spins_left = 10
        self.assertEqual(30, policy.item_economy_components(engine, {"per_spin_token": {"amount": 1}})["item_recurring_tokens"])

    def test_event_income_uses_actual_named_events_and_signed_rewards(self):
        engine = self.engine()
        policy = ItemEconomyStrategy()
        self.assertEqual(24, policy.item_economy_components(engine, {"event_bonus": {"removed": 2}})["item_event_income"])
        self.assertEqual(-24, policy.item_economy_components(engine, {"event_bonus": {"removed": -2}})["item_event_income"])
        self.assertEqual(0, policy.item_economy_components(engine, {"event_bonus": {"unseen": 100}})["item_event_income"])

    def test_event_rewards_handle_gold_tokens_and_every_without_fake_choices(self):
        engine = self.engine()
        row = {"event_bonus_every": {"ingredient_added": {"every": 3, "amount": 2, "tokens": {"remove": 1}}}}
        self.assertEqual(27, ItemEconomyStrategy().item_economy_components(engine, row)["item_event_rewards"])
        row["event_bonus_every"]["ingredient_added"] = {"every": 2, "choice_extra": 1}
        self.assertEqual(0, ItemEconomyStrategy().item_economy_components(engine, row)["item_event_rewards"])

    def test_order_savings_is_future_rescue_not_existing_cash(self):
        engine = self.engine()
        row = {"order_savings": {"deposit_on_complete": 5, "withdraw_before_failure": True}}
        before = ItemEconomyStrategy().item_economy_components(engine, row)
        self.assertEqual(3, before["item_order_savings"])
        engine.s.stats["item_storage"] = {"unrelated": 1000000}
        self.assertEqual(before, ItemEconomyStrategy().item_economy_components(engine, row))
        row["order_savings"]["withdraw_before_failure"] = False
        self.assertEqual(0, ItemEconomyStrategy().item_economy_components(engine, row)["item_order_savings"])

    def test_legacy_or_zero_history_has_no_invented_event_evidence(self):
        engine = self.engine()
        row = {"event_bonus": {"removed": 5}}
        engine.s.spin = 0
        self.assertEqual(0, ItemEconomyStrategy().item_economy_components(engine, row)["item_event_income"])
        engine.s.spin = 10
        engine.s.stats.pop("event_counts")
        self.assertEqual(0, ItemEconomyStrategy().item_economy_components(engine, row)["item_event_income"])

    def test_scoring_is_pure_and_independent_of_definition_ids(self):
        engine = self.engine()
        policy = ItemEconomyStrategy()
        row = {"id": "synthetic_one", "rarity": 1, "description": "x", "event_bonus": {"removed": 3}}
        before = deepcopy(engine.s.to_dict())
        a = policy.item_economy_components(engine, row)
        row["id"] = "renamed"
        self.assertEqual(a, policy.item_economy_components(engine, row))
        self.assertEqual(before, engine.s.to_dict())

    def test_unmodelled_items_and_ingredient_scores_keep_baseline(self):
        engine = self.engine()
        old, new = ContentActionStrategy(), ItemEconomyStrategy()
        for item in ("worker", "prism_calibrator", "magic_filter"):
            self.assertEqual(old.score(engine, "item", item), new.score(engine, "item", item))
        for ingredient in engine.catalog.ingredients:
            self.assertEqual(old.score_components(engine, "ingredient", ingredient), new.score_components(engine, "ingredient", ingredient))
        self.assertEqual(6, old.score(engine, "item", "auto_reroller"))
        self.assertEqual(21, new.score(engine, "item", "auto_reroller"))

    def test_registration_and_seed_reproducibility(self):
        self.assertIsInstance(strategy_from_name("heuristic-v2-content-v4"), ItemEconomyStrategy)
        self.assertIsInstance(strategy_from_name("heuristic-v2-content-v3"), ContentActionStrategy)
        one = run_batch(2, 20261015, 7, strategy=ItemEconomyStrategy())
        two = run_batch(2, 20261015, 7, strategy=ItemEconomyStrategy())
        self.assertEqual(one.to_dict(), two.to_dict())
        self.assertEqual(0, one.summary["aborted"])
