from __future__ import annotations

import unittest

from crucible_echoes.engine import GameEngine, GameError


class OrdersTokensEssencesTests(unittest.TestCase):
    def test_order_curve_and_cumulative_difficulty_bonuses(self) -> None:
        engine = GameEngine(); engine.new_game(1, difficulty=10)
        self.assertEqual((285, 7), engine.current_order_for(5, 1, {}))
        expected = {7: 375, 8: 450, 9: 600, 10: 650, 11: 700}
        for order_number, amount in expected.items():
            actual, _ = engine.current_order_for(order_number - 1, 10, {})
            self.assertEqual(amount, actual)
        self.assertEqual((1350, 15), engine.current_order_for(12, 10, {}))
        self.assertEqual((1000, 10), engine.current_order_for(12, 1, {}))

    def test_d11_to_d15_inherit_final_order_and_order12_rules(self) -> None:
        expected_final = {10: 1350, 11: 1425, 12: 1425, 13: 1425, 14: 1500, 15: 1500}
        for difficulty, amount in expected_final.items():
            engine = GameEngine(); engine.new_game(1, difficulty=difficulty)
            self.assertEqual((amount, 15), engine.current_order_for(12, difficulty, {}))
        d13 = GameEngine(); d13.new_game(1, difficulty=13)
        self.assertEqual((800, 10), d13.current_order_for(11, 13, {}))
        self.assertAlmostEqual(0.95, d13.current_rarity_multiplier())

    def test_d12_slag_is_zero_value_and_lasts_two_extra_rounds(self) -> None:
        d12 = GameEngine(); d12.new_game(1, difficulty=12)
        d12.s.ingredients.clear()
        d12.s.items.append("magnifier")
        slag = d12.add_ingredient("slag", emit=False)
        self.assertIsNotNone(slag)
        d12._board = [slag]
        d12._coords = [(0, 0)]
        self.assertEqual([0], d12._base_values())

        slag.age = 33
        d12._values = [0]
        d12._run_active_effects()
        self.assertIn(slag, d12.s.ingredients)
        slag.age = 35
        d12._run_active_effects()
        self.assertNotIn(slag, d12.s.ingredients)

    def test_d15_post_order_deduction_never_causes_negative_or_failure(self) -> None:
        engine = GameEngine(); engine.new_game(1, difficulty=15)
        engine.s.order_index = 3
        engine.s.spins_left = 1
        engine.s.gold = 200
        engine.s.ingredients.clear()
        engine.spin()
        self.assertEqual("playing", engine.s.status)
        self.assertEqual(43, engine.s.gold)

        exact = GameEngine(); exact.new_game(2, difficulty=15)
        exact.s.order_index = 3
        exact.s.spins_left = 1
        exact.s.gold = 150
        exact.s.ingredients.clear()
        exact.spin()
        self.assertEqual("playing", exact.s.status)
        self.assertEqual(0, exact.s.gold)

    def test_initial_slag_and_interval_rules(self) -> None:
        for difficulty, count in ((1,0),(5,1),(6,2),(8,3),(10,3),(12,3),(15,3)):
            engine = GameEngine(); engine.new_game(1, difficulty)
            self.assertEqual(count, sum(x.def_id == "slag" for x in engine.s.ingredients))
        self.assertEqual(15, GameEngine.slag_interval(7))
        self.assertEqual(15, GameEngine.slag_interval(8))
        self.assertEqual(15, GameEngine.slag_interval(10))

    def test_even_order_awards_tokens_with_cumulative_per_token_rules(self) -> None:
        for difficulty, expected_remove, expected_roll, expected_essence in ((1,2,2,2),(4,1,1,2),(10,1,1,1)):
            engine = GameEngine(); engine.new_game(1, difficulty)
            engine.s.order_index = 3
            engine.s.spins_left = 1
            engine.s.gold = 9999
            engine.spin()
            self.assertEqual(expected_roll, engine.s.tokens["roll"])
            self.assertEqual(expected_remove, engine.s.tokens["remove"])
            self.assertEqual(0, engine.s.tokens["essence"])
            essence_choices = [x for x in engine.s.pending if x.kind == "essence"]
            self.assertEqual(expected_essence, len(essence_choices))

    def test_order_log_keeps_order_amount_after_token_rewards(self) -> None:
        engine = GameEngine(); engine.new_game(1)
        engine.s.order_index = 3  # fourth order: 150g
        engine.s.spins_left = 1
        engine.s.gold = 200

        engine._settle_order()

        self.assertEqual(50, engine.s.gold)
        self.assertIn("完成第4份订单，支付150g。", engine.s.last_log)
        self.assertNotIn("完成第4份订单，支付2g。", engine.s.last_log)

    def test_order_completion_only_has_guaranteed_ingredient_reward_by_default(self) -> None:
        engine = GameEngine(); engine.new_game(1)
        amount, _ = engine.current_order()
        engine.s.spins_left = 1
        engine.s.gold = amount

        engine.spin()

        ingredient_choices = [choice for choice in engine.s.pending if choice.kind == "ingredient"]
        self.assertEqual(["order_guarantee"], [choice.source for choice in ingredient_choices])

    def test_order_appendix_adds_one_normal_ingredient_reward(self) -> None:
        engine = GameEngine(); engine.new_game(1)
        engine.add_item("order_appendix")
        amount, _ = engine.current_order()
        engine.s.spins_left = 1
        engine.s.gold = amount

        engine.spin()

        ingredient_choices = [choice for choice in engine.s.pending if choice.kind == "ingredient"]
        self.assertEqual(["order_guarantee", "order_appendix"], [choice.source for choice in ingredient_choices])

    def test_normal_spin_ingredient_reward_is_unchanged(self) -> None:
        engine = GameEngine(); engine.new_game(1)
        engine.s.spins_left = 2

        engine.spin()

        ingredient_choices = [choice for choice in engine.s.pending if choice.kind == "ingredient"]
        self.assertEqual(["spin"], [choice.source for choice in ingredient_choices])

    def test_duplicate_order_appendices_do_not_stack(self) -> None:
        engine = GameEngine(); engine.new_game(1)
        engine.s.items.extend(["order_appendix", "order_appendix"])
        amount, _ = engine.current_order()
        engine.s.spins_left = 1
        engine.s.gold = amount

        engine.spin()

        appendix_choices = [choice for choice in engine.s.pending if choice.source == "order_appendix"]
        self.assertEqual(1, len(appendix_choices))

    def test_order_appendix_does_not_trigger_on_failed_order(self) -> None:
        engine = GameEngine(); engine.new_game(1)
        engine.add_item("order_appendix")

        engine._settle_order()

        self.assertEqual("lost", engine.s.status)
        self.assertFalse(any(choice.source == "order_appendix" for choice in engine.s.pending))

    def test_d10_final_order_requires_extra_thirteen_order(self) -> None:
        engine = GameEngine(); engine.new_game(1, difficulty=10)
        engine.s.order_index = 11
        engine.s.spins_left = 1
        engine.s.gold = 1350
        engine.spin()
        self.assertEqual("playing", engine.s.status)
        self.assertEqual(12, engine.s.order_index)
        self.assertEqual(15, engine.s.spins_left)
        self.assertEqual((1350, 15), engine.current_order())

    def test_reroll_and_removal_tokens_are_consumed(self) -> None:
        engine = GameEngine(); engine.new_game(4)
        engine.spin()
        old = list(engine.s.pending[0].offers)
        engine.s.tokens["roll"] = 1
        engine.reroll()
        self.assertEqual(0, engine.s.tokens["roll"])
        self.assertNotEqual(old, engine.s.pending[0].offers)
        engine.skip()
        engine.s.tokens["remove"] = 1
        size = len(engine.s.ingredients)
        engine.remove(1)
        self.assertEqual(size - 1, len(engine.s.ingredients))
        self.assertEqual(0, engine.s.tokens["remove"])

    def test_slag_cannot_be_manually_removed(self) -> None:
        engine = GameEngine(); engine.new_game(5, difficulty=5)
        slag_index = next(i for i,x in enumerate(engine.s.ingredients,1) if x.def_id == "slag")
        engine.s.tokens["remove"] = 1
        with self.assertRaises(GameError):
            engine.remove(slag_index)

    def test_essence_condition_triggers_and_is_consumed(self) -> None:
        engine = GameEngine(); engine.new_game(9)
        engine.s.ingredients.clear()
        for def_id in ("test_tube", "measuring_cylinder", "flask"):
            engine.add_ingredient(def_id, emit=False)
        engine.add_essence("test_tube_rack_essence")
        engine.spin()
        self.assertNotIn("test_tube_rack_essence", engine.s.essences)
        self.assertIn("test_tube_rack_essence", engine.s.consumed_essences)
        self.assertTrue(all(x.permanent_bonus == 2 for x in engine.s.ingredients))


if __name__ == "__main__":
    unittest.main()
