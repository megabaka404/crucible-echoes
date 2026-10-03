from copy import deepcopy
import unittest

from crucible_echoes.engine import GameEngine
from crucible_echoes.item_strategy import ItemEconomyStrategy
from crucible_echoes.model import PendingChoice
from crucible_echoes.simulation import run_batch
from tools.item_decision_diagnostics import ObservedItemPolicy, selector_count, token_reward_types, token_usage_summary


class ItemDecisionDiagnosticTests(unittest.TestCase):
    def test_observation_keeps_state_rng_and_selected_action(self):
        engine = GameEngine()
        engine.new_game(20261017)
        engine.s.ingredients.clear()
        engine.s.spin = 10
        engine.s.stats["event_counts"] = {"removed_tag:cat": 20}
        choice = PendingChoice("item", ["cat_litter", "worker"])
        before = deepcopy(engine.s.to_dict())
        observer = ObservedItemPolicy()
        self.assertEqual(ItemEconomyStrategy().choose(engine, choice), observer.choose(engine, choice))
        self.assertEqual(before, engine.s.to_dict())
        trace = observer.traces[0]
        self.assertEqual("cat_litter", trace["selected"])
        self.assertEqual("worker", trace["baseline_recommendation"])
        self.assertEqual(0, trace["offers"][0]["events"]["removed_tag:cat"]["current_selector_count"])
        self.assertEqual(2, trace["offers"][0]["events"]["removed_tag:cat"]["historical_per_spin"])
        self.assertEqual(engine.s.tokens, trace["tokens"])
        self.assertEqual(engine.s.spins_left, trace["spins_left"])

    def test_token_reward_types_follow_effects_not_ids_and_exclude_nonpositive(self):
        row = {"id": "anything", "per_spin_token": {"token": "roll"},
               "periodic_token": {"token": "essence", "amount": 2},
               "event_bonus_every": {"removed": {"tokens": {"remove": 1, "roll": 0, "negative": -1}}}}
        self.assertEqual(["essence", "remove", "roll"], token_reward_types(row))
        row["id"] = "renamed"
        self.assertEqual(["essence", "remove", "roll"], token_reward_types(row))
        self.assertEqual([], token_reward_types({"per_spin_token": {"token": "roll", "amount": 0}}))

    def test_selectors_are_data_driven_and_unknown_names_remain_unknown(self):
        engine = GameEngine()
        engine.new_game(41)
        engine.s.ingredients.clear()
        cat_id = next(row["id"] for row in engine.catalog.ingredients.values() if "cat" in row.get("tags", []))
        engine.add_ingredient(cat_id, emit=False)
        engine.add_ingredient(cat_id, emit=False)
        engine.add_ingredient("ash", emit=False)
        self.assertEqual(2, selector_count(engine, "removed_tag:cat"))
        self.assertEqual(1, selector_count(engine, "removed_ids:ash,rust,alchemy_scrap"))
        self.assertIsNone(selector_count(engine, "opaque_future_event"))

    def test_traced_and_untraced_complete_batches_are_identical(self):
        traced = ObservedItemPolicy()
        a = run_batch(2, 20261017, 7, strategy=traced)
        b = run_batch(2, 20261017, 7, strategy=ItemEconomyStrategy())
        self.assertEqual(a.to_dict(), b.to_dict())
        self.assertTrue(traced.traces)
        self.assertEqual(0, a.summary["aborted"])

    def test_usage_summary_reconciles_actual_records_without_mutation(self):
        batch = run_batch(2, 20261018, 7, strategy=ObservedItemPolicy())
        before = deepcopy(batch.to_dict())
        usage = token_usage_summary(batch)
        self.assertEqual(before, batch.to_dict())
        self.assertEqual(2, len(usage["games"]))
        for key, event in (("roll_actions", "rolls"), ("delete_actions", "deletes")):
            self.assertEqual(sum(len(g["strategy_events"][event]) for g in batch.games_detail), usage["totals"][key])
        for token in ("roll", "remove", "essence"):
            self.assertEqual(sum(g["final_attributes"]["tokens"][token] for g in batch.games_detail), usage["totals"]["final_tokens"][token])
        self.assertIn("untracked", usage["coverage"])
        self.assertNotIn("essence_spent", usage["totals"])
