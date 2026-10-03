from __future__ import annotations

import unittest

from crucible_echoes.engine import GameEngine
from crucible_echoes.model import GameState, PendingChoice


class ChoiceScopeRegressionTests(unittest.TestCase):
    def fresh(self):
        engine = GameEngine()
        engine.new_game(71)
        engine.s.gold = 10000
        engine.s.spins_left = 99
        return engine

    def choice(self):
        return PendingChoice(kind="ingredient", offers=["water"])

    def finish_rewards(self, engine):
        while engine.s.pending:
            engine.choose(1)

    def test_reroll_keeps_already_expanded_item_candidate_count(self):
        engine = self.fresh()
        engine.s.items.append("instant_meal")
        engine.s.pending.append(engine.make_choice("item", source="order"))
        engine.s.tokens["roll"] = 3
        self.assertEqual(4, len(engine.s.pending[0].offers))
        for _ in range(3):
            engine = GameEngine().bind(GameState.from_dict(engine.s.to_dict()))
            engine.reroll()
            self.assertEqual(4, len(engine.s.pending[0].offers))

    def test_reroll_preserves_next_choice_extra_from_skipping_queued_reward(self):
        engine = self.fresh()
        engine.s.items.append("old_catalog")
        engine.s.pending = [engine.make_choice("ingredient"), engine.make_choice("ingredient")]
        engine.s.tokens["roll"] = 2
        engine.skip()
        for _ in range(2):
            engine.reroll()
            self.assertEqual(3, len(engine.s.pending[0].offers))
            self.assertEqual(1, engine.s.flags["ingredient_choice_extra"])
        engine.choose(1)
        next_choice = engine.make_choice("ingredient")
        self.assertEqual(4, len(next_choice.offers))
        self.assertNotIn("ingredient_choice_extra", engine.s.flags)

    def test_stabilized_before_choice_essence_waits_for_next_new_group(self):
        engine = self.fresh()
        engine.s.items.append("essence_stabilizer")
        engine.add_essence("spare_beaker_essence")
        engine.s.pending.append(engine.make_choice("ingredient"))
        self.assertEqual(5, len(engine.s.pending[0].offers))
        engine.s.tokens["roll"] = 1
        # The retained essence becomes eligible in a later round, but this
        # queued reward still belongs to its original choice instance.
        engine.s.spin += 1
        engine.reroll()
        self.assertEqual(5, len(engine.s.pending[0].offers))
        self.assertEqual(1, engine.s.stats["essence_hits"]["spare_beaker_essence"])
        engine.choose(1)
        self.assertEqual(5, len(engine.make_choice("ingredient").offers))
        self.assertIn("spare_beaker_essence", engine.s.consumed_essences)

    def test_plain_reroll_keeps_previous_draw_rng_sequence(self):
        for kind in ("ingredient", "item"):
            engine = self.fresh()
            engine.s.pending.append(engine.make_choice(kind))
            engine.s.tokens["roll"] = 1
            engine._sync_rng()
            reference = GameEngine().bind(GameState.from_dict(engine.s.to_dict()))
            expected = reference.make_choice(kind, count=3)
            engine.reroll()
            self.assertEqual(expected.offers, engine.s.pending[0].offers)
            self.assertEqual(reference.r.state, engine.r.state)

    def test_two_different_choices_do_not_share_reroll_progress(self):
        engine = self.fresh()
        engine.add_essence("auto_reroller_essence")
        engine.s.pending = [self.choice(), self.choice()]
        engine.s.tokens["roll"] = 2
        engine.reroll()
        engine.skip()
        engine.reroll()
        self.assertIn("auto_reroller_essence", engine.s.essences)
        self.assertEqual(0, engine.s.tokens["roll"])

    def test_same_choice_progress_survives_json_reload(self):
        engine = self.fresh()
        engine.s.pending = [self.choice()]
        engine.add_essence("auto_reroller_essence")
        engine.s.tokens["roll"] = 2
        engine.reroll()
        uid = engine.s.pending[0].details["choice_uid"]
        engine = GameEngine().bind(GameState.from_dict(engine.s.to_dict()))
        engine.reroll()
        self.assertEqual(uid, engine.s.pending[0].details["choice_uid"])
        self.assertEqual(5, engine.s.tokens["roll"])
        self.assertIn("auto_reroller_essence", engine.s.consumed_essences)

    def test_acquisition_after_earlier_rerolls_resets_baseline(self):
        engine = self.fresh()
        engine.s.pending = [self.choice()]
        engine.s.tokens["roll"] = 4
        engine.reroll()
        engine.reroll()
        engine.add_essence("auto_reroller_essence")
        engine.reroll()
        self.assertIn("auto_reroller_essence", engine.s.essences)
        engine.reroll()
        self.assertEqual(5, engine.s.tokens["roll"])

    def test_extra_choices_apply_for_all_three_next_spins(self):
        engine = self.fresh()
        engine.s.flags["extra_choice_spins"] = 3
        counts = []
        for _ in range(4):
            engine.spin()
            counts.append(sum(choice.source == "essence" for choice in engine.s.pending))
            self.finish_rewards(engine)
            engine = GameEngine().bind(GameState.from_dict(engine.s.to_dict()))
        self.assertEqual([1, 1, 1, 0], counts)
        self.assertEqual(0, engine.s.flags["extra_choice_spins"])

    def test_two_picks_in_one_round_are_not_two_rounds(self):
        engine = self.fresh()
        engine.add_essence("frozen_meal_essence")
        engine.spin()
        engine.s.pending.append(self.choice())
        self.finish_rewards(engine)
        self.assertIn("frozen_meal_essence", engine.s.essences)
        self.assertEqual(1, engine.s.stats["choice_round_streak"]["length"])

    def test_skip_between_picks_breaks_the_round_streak(self):
        engine = self.fresh()
        engine.add_essence("frozen_meal_essence")
        engine.spin()
        engine.s.pending.extend([self.choice(), self.choice()])
        engine.choose(1)
        engine.skip()
        engine.choose(1)
        self.assertIn("frozen_meal_essence", engine.s.essences)
        self.assertEqual(0, engine.s.stats["choice_round_streak"]["length"])
        for turn in range(2):
            engine.spin()
            self.finish_rewards(engine)
            self.assertEqual(turn == 1, "frozen_meal_essence" in engine.s.consumed_essences)

    def test_two_successive_reward_rounds_trigger_once_then_three_rewards(self):
        engine = self.fresh()
        engine.add_essence("frozen_meal_essence")
        for _ in range(2):
            engine.spin()
            self.finish_rewards(engine)
            engine = GameEngine().bind(GameState.from_dict(engine.s.to_dict()))
        self.assertIn("frozen_meal_essence", engine.s.consumed_essences)
        self.assertEqual(3, engine.s.flags["extra_choice_spins"])
        for remaining in (2, 1, 0):
            engine.spin()
            self.assertEqual(1, sum(x.source == "essence" for x in engine.s.pending))
            self.finish_rewards(engine)
            self.assertEqual(remaining, engine.s.flags["extra_choice_spins"])

    def test_prior_rounds_do_not_count_before_acquisition(self):
        engine = self.fresh()
        for _ in range(3):
            engine.spin()
            self.finish_rewards(engine)
        engine.add_essence("frozen_meal_essence")
        engine.check_essences()
        self.assertIn("frozen_meal_essence", engine.s.essences)
        engine.spin()
        self.finish_rewards(engine)
        self.assertIn("frozen_meal_essence", engine.s.essences)
        engine.spin()
        self.finish_rewards(engine)
        self.assertIn("frozen_meal_essence", engine.s.consumed_essences)

    def test_stabilized_essence_restarts_all_scope_baselines(self):
        engine = self.fresh()
        engine.s.items.append("essence_stabilizer")
        engine.add_essence("frozen_meal_essence")
        for _ in range(2):
            engine.spin()
            self.finish_rewards(engine)
        self.assertEqual(1, engine.s.stats["essence_hits"]["frozen_meal_essence"])
        engine.spin()
        self.finish_rewards(engine)
        self.assertIn("frozen_meal_essence", engine.s.essences)
        engine.spin()
        self.finish_rewards(engine)
        self.assertIn("frozen_meal_essence", engine.s.consumed_essences)

    def test_old_save_has_safe_new_counter_defaults_without_rng_draws(self):
        engine = self.fresh()
        engine.s.pending = [self.choice()]
        data = engine.s.to_dict()
        data["stats"].pop("choice_round_streak", None)
        data["stats"].pop("choice_round_progress", None)
        data["stats"].pop("next_choice_uid", None)
        engine = GameEngine().bind(GameState.from_dict(data))
        before = engine.r.state
        engine.add_essence("auto_reroller_essence")
        engine.add_essence("frozen_meal_essence")
        engine.check_essences()
        self.assertEqual(before, engine.r.state)
        self.assertEqual(2, len(engine.s.essences))


if __name__ == "__main__":
    unittest.main()
