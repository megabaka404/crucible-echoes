from __future__ import annotations

from collections import defaultdict
import json
import unittest

from crucible_echoes.engine import GameEngine
from crucible_echoes.model import GameState


class ProtocolPrismBanRegressionTests(unittest.TestCase):
    def fresh(self, mode="none"):
        engine = GameEngine()
        engine.new_game(5102, fun_mode=mode)
        engine.s.ingredients.clear()
        return engine

    def reload(self, engine):
        engine._sync_rng()
        return GameEngine().bind(GameState.from_dict(json.loads(json.dumps(engine.s.to_dict()))))

    def prism_board(self, *ids):
        engine = self.fresh()
        engine._board = [engine.add_ingredient(def_id, emit=False) for def_id in (*ids, "water")]
        engine._coords = [(0, index) for index in range(len(engine._board))]
        engine.r.choice = lambda _choices: (0, 1)
        return engine

    def ban(self, engine):
        engine._apply_essence_effect(engine.catalog.essences["ban_essence"]["effect"], "test")

    def test_protocol_next_order_penalty_lasts_until_that_order_is_paid(self):
        engine = self.fresh()
        engine.s.items.append("emergency_protocol")
        engine.s.gold = 0
        engine._settle_order()
        self.assertEqual((63, 5), engine.current_order())
        self.assertTrue(engine.s.flags["next_order_penalty"])
        engine.s.pending.clear()
        engine.s.gold = 63
        engine._settle_order()
        self.assertEqual(0, engine.s.gold)
        self.assertEqual((100, 6), engine.current_order())
        self.assertNotIn("next_order_penalty", engine.s.flags)

    def test_protocol_essence_cancels_and_consumes_its_flag(self):
        engine = self.fresh()
        engine.add_essence("emergency_protocol_essence")
        engine.s.items.append("emergency_protocol")
        engine.s.gold = 0
        engine._settle_order()
        self.assertEqual((50, 5), engine.current_order())
        self.assertIn("emergency_protocol_essence", engine.s.consumed_essences)
        self.assertNotIn("cancel_order_penalty", engine.s.flags)
        self.assertNotIn("next_order_penalty", engine.s.flags)

    def test_protocol_penalty_and_rng_survive_json_reload(self):
        engine = self.fresh()
        engine.s.items.append("emergency_protocol")
        engine.s.gold = 0
        engine._settle_order()
        resumed = self.reload(engine)
        self.assertEqual(engine.s.to_dict(), resumed.s.to_dict())
        self.assertEqual((63, 5), resumed.current_order())
        self.assertEqual(engine.r.state, resumed.r.state)

    def test_coffee_extension_does_not_consume_existing_penalty(self):
        engine = self.fresh()
        engine.s.flags["next_order_penalty"] = True
        engine.s.items.append("emergency_coffee")
        engine.s.gold = 0
        engine._settle_order()
        self.assertEqual(0, engine.s.order_index)
        self.assertEqual(1, engine.s.spins_left)
        self.assertEqual((32, 5), engine.current_order())

    def test_protocol_rescue_in_endless_penalizes_only_next_target(self):
        engine = self.fresh()
        engine._resolve_run_end_choice("enter_endless")
        engine.s.items.append("emergency_protocol")
        engine.s.gold = 0
        engine._settle_order()
        self.assertEqual(1500, engine.s.endless_target)
        self.assertEqual((1875, 10), engine.current_order())
        engine.s.pending.clear()
        engine.s.gold = 1875
        engine._settle_order()
        self.assertEqual((2250, 10), engine.current_order())

    def test_outstanding_penalty_carries_into_endless_but_peace_stays_zero(self):
        for mode, expected in (("enter_endless", (1250, 10)), ("enter_peace", (0, 7))):
            with self.subTest(mode=mode):
                engine = self.fresh()
                engine.s.order_index = 11
                engine.s.items.append("emergency_protocol")
                engine.s.gold = 0
                engine._settle_order()
                self.assertTrue(engine.s.flags["next_order_penalty"])
                engine.s.pending.clear()
                engine._resolve_run_end_choice(mode)
                self.assertEqual(expected, engine.current_order())

    def test_penalty_rounding_uses_exact_integers_for_large_endless_targets(self):
        engine = self.fresh()
        engine._resolve_run_end_choice("enter_endless")
        engine.s.endless_target = 10**22 + 1
        engine.s.flags["next_order_penalty"] = True
        self.assertEqual((engine.s.endless_target * 5 + 3) // 4, engine.current_order()[0])

    def test_same_prism_copies_and_extra_directions_count_one_type(self):
        engine = self.prism_board("copper_prism", "copper_prism", "copper_prism")
        engine.s.items.append("prism_calibrator")
        engine.add_essence("prism_calibrator_essence")
        engine._apply_multipliers([0, 0, 0, 1])
        self.assertEqual(1, engine._round_events["prism_type"])
        self.assertGreater(engine._round_events["adjacency"], 3)
        engine.check_essences()
        self.assertIn("prism_calibrator_essence", engine.s.essences)

    def test_three_types_trigger_temporary_prism_multiplier_then_decay(self):
        engine = self.prism_board("copper_prism", "silver_prism", "gold_prism")
        engine.add_essence("prism_calibrator_essence")
        self.assertEqual(24, engine._apply_multipliers([0, 0, 0, 1])[-1])
        engine.check_essences()
        self.assertIn("prism_calibrator_essence", engine.s.consumed_essences)
        self.assertEqual(1, engine.s.flags["prism_multiplier_spins"])
        self.assertEqual(81, engine._apply_multipliers([0, 0, 0, 1])[-1])
        engine._decay_flags()
        self.assertEqual(0, engine.s.flags["prism_multiplier_spins"])
        self.assertEqual(24, engine._apply_multipliers([0, 0, 0, 1])[-1])

    def test_prism_baseline_excludes_types_triggered_before_essence_acquisition(self):
        engine = self.prism_board("copper_prism", "silver_prism", "gold_prism")
        engine._apply_multipliers([0, 0, 0, 1])
        engine.add_essence("prism_calibrator_essence")
        engine._apply_multipliers([0, 0, 0, 1])
        engine.check_essences()
        self.assertIn("prism_calibrator_essence", engine.s.essences)
        engine.s.spin += 1
        engine._round_events = defaultdict(int)
        engine._apply_multipliers([0, 0, 0, 1])
        engine.check_essences()
        self.assertIn("prism_calibrator_essence", engine.s.consumed_essences)

    def test_distinct_round_markers_survive_reload_without_rng(self):
        engine = self.prism_board("copper_prism", "silver_prism", "gold_prism")
        engine._emit_distinct_round_event("prism_type", "copper_prism")
        resumed = self.reload(engine)
        before_rng = resumed.r.state
        resumed._emit_distinct_round_event("prism_type", "copper_prism")
        self.assertEqual(1, resumed._round_events["prism_type"])
        self.assertEqual(before_rng, resumed.r.state)

    def test_ban_materializes_existing_generators_once_and_future_gains(self):
        engine = self.fresh()
        existing = engine.add_ingredient("magic_magic", emit=False, permanent_bonus=5)
        before_rng = engine.r.state
        self.ban(engine)
        self.assertEqual(6, existing.permanent_bonus)
        engine._apply_generation_bonus_to_instance(existing)
        self.assertEqual(6, existing.permanent_bonus)
        future = engine.add_ingredient("summoner", emit=False)
        self.assertEqual(1, future.permanent_bonus)
        self.assertTrue(engine.ingredient_generation_disabled())
        self.assertEqual(before_rng, engine.r.state)

    def test_generator_transform_does_not_repeat_ban_growth_and_resets_mechanics(self):
        engine = self.fresh()
        source = engine.add_ingredient("magic_magic", emit=False, permanent_bonus=5)
        self.ban(engine)
        source.age = source.counter = 20
        source.flags["old_counter"] = 3
        engine._transform(source, "summoner")
        self.assertEqual(6, source.permanent_bonus)
        self.assertEqual({"ingredient_generation_bonus_applied": 1}, source.flags)
        self.assertEqual((0, 0), (source.age, source.counter))
        engine._transform(source, "magic_magic")
        self.assertEqual(6, source.permanent_bonus)

    def test_generator_non_generator_generator_chain_keeps_global_growth_attribution(self):
        engine = self.fresh()
        source = engine.add_ingredient("magic_magic", emit=False)
        self.ban(engine)
        engine._transform(source, "water")
        self.assertEqual(1, source.permanent_bonus)
        engine._transform(source, "summoner")
        self.assertEqual(1, source.permanent_bonus)

    def test_generator_mutation_preserves_one_ban_bonus_and_resets_local_state(self):
        engine = self.fresh("mutation")
        source = engine.add_ingredient("magic_magic", emit=False, permanent_bonus=5)
        self.ban(engine)
        source.flags["old_counter"] = 3
        source.stored_gold = 9
        engine.r.random = lambda: 0.5
        engine.r.weighted_choice = lambda _rows: "summoner"
        engine._mutate_instance(source)
        self.assertEqual(6, source.permanent_bonus)
        self.assertEqual({"ingredient_generation_bonus_applied": 1}, source.flags)
        self.assertEqual(0, source.stored_gold)
        engine._mutate_instance(source)
        self.assertEqual(6, source.permanent_bonus)

    def test_legacy_unmaterialized_ban_bonus_is_preserved_before_transformation(self):
        engine = self.fresh()
        source = engine.add_ingredient("magic_magic", emit=False, permanent_bonus=5)
        engine.s.flags.update(ingredient_generation_permanently_disabled=True, ingredient_generation_bonus=1)
        self.assertNotIn("ingredient_generation_bonus_applied", source.flags)
        engine._transform(source, "summoner")
        self.assertEqual(6, source.permanent_bonus)
        engine._transform(source, "magic_magic")
        self.assertEqual(6, source.permanent_bonus)

    def test_minimal_ban_growth_doubles_once_without_transform_reapplication(self):
        engine = self.fresh("minimal")
        source = engine.add_ingredient("magic_magic", emit=False)
        self.ban(engine)
        self.assertEqual(2, source.permanent_bonus)
        engine._transform(source, "summoner")
        self.assertEqual(2, source.permanent_bonus)

    def test_ban_attribution_survives_save_and_later_identity_changes(self):
        engine = self.fresh()
        source = engine.add_ingredient("magic_magic", emit=False)
        self.ban(engine)
        resumed = self.reload(engine)
        self.assertEqual(engine.s.to_dict(), resumed.s.to_dict())
        resumed._transform(resumed.s.ingredients[0], "summoner")
        self.assertEqual(1, resumed.s.ingredients[0].permanent_bonus)


if __name__ == "__main__":
    unittest.main()
