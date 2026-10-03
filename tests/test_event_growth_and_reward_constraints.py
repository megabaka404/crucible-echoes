from __future__ import annotations

import unittest

from crucible_echoes.engine import GameEngine
from crucible_echoes.model import GameState


class EventGrowthAndRewardConstraintsTests(unittest.TestCase):
    def fresh(self, seed=0, mode="none"):
        engine = GameEngine()
        engine.new_game(seed, fun_mode=mode)
        engine.s.ingredients.clear()
        engine.s.items.clear()
        engine.s.pending.clear()
        engine.s.gold = 10000
        engine.s.spins_left = 99
        return engine

    def board(self, source, listener, adjacent=True, mode="none"):
        engine = self.fresh(mode=mode)
        a = engine.add_ingredient(source, emit=False)
        b = engine.add_ingredient(listener, emit=False)
        engine._board = [a, b]
        engine._coords = [(0, 0), (0, 1) if adjacent else (2, 3)]
        engine._values = [1, 1]
        return engine, a, b

    def test_compass_preserves_fixed_rarity_across_seeds(self):
        for seed in range(40):
            engine = self.fresh(seed)
            engine.s.items.append("lucky_compass")
            choice = engine.make_choice("ingredient", fixed_rarity=3)
            self.assertEqual([3, 3, 3], [engine.catalog.ingredients[x]["rarity"] for x in choice.offers])

    def test_compass_preserves_each_slot_minimum(self):
        for seed in range(40):
            engine = self.fresh(seed)
            engine.s.items.append("lucky_compass")
            choice = engine.make_choice("ingredient", minimums=[3, 2, 2])
            for minimum, def_id in zip([3, 2, 2], choice.offers):
                self.assertGreaterEqual(engine.catalog.ingredients[def_id]["rarity"], minimum)

    def test_fixed_and_minimum_constraints_survive_reroll_and_save(self):
        for args in ({"fixed_rarity": 3}, {"minimums": [3, 2, 2]}):
            engine = self.fresh()
            engine.s.items.append("lucky_compass")
            engine.s.pending.append(engine.make_choice("ingredient", **args))
            engine.s.tokens["roll"] = 3
            for _ in range(3):
                engine = GameEngine().bind(GameState.from_dict(engine.s.to_dict()))
                engine.reroll()
                tiers = [engine.catalog.ingredients[x]["rarity"] for x in engine.s.pending[0].offers]
                if "fixed_rarity" in args:
                    self.assertEqual([3, 3, 3], tiers)
                else:
                    self.assertTrue(all(t >= m for t, m in zip(tiers, [3, 2, 2])))

    def test_legacy_choice_without_constraints_still_rerolls(self):
        engine = self.fresh()
        choice = engine.make_choice("ingredient")
        choice.details.clear()
        engine.s.pending.append(choice)
        engine.s.tokens["roll"] = 1
        engine.reroll()
        self.assertEqual(3, len(engine.s.pending[0].offers))

    def test_slime_grows_once_per_adjacent_transformation_not_globally(self):
        for adjacent in (True, False):
            engine, source, listener = self.board("water", "alchemy_slime", adjacent)
            rng_before = engine.r.state
            engine._transform(source, "ash")
            engine._run_script(1, listener, "slime")
            self.assertEqual(1 if adjacent else 0, listener.permanent_bonus)
            self.assertEqual(rng_before, engine.r.state)
            engine._transform(source, "water")
            self.assertEqual(2 if adjacent else 0, listener.permanent_bonus)

    def test_glassmaker_only_grows_for_adjacent_glass_equipment(self):
        for source_id, adjacent, expected in (("broken_flask", True, 1), ("broken_flask", False, 0), ("stone", True, 0), ("glass_shard", True, 0)):
            engine, source, listener = self.board(source_id, "glassmaker", adjacent)
            engine._remove(source, "shattered", 0)
            engine._run_script(1, listener, "glassmaker")
            self.assertEqual(expected, listener.permanent_bonus)

    def test_mouse_grows_on_each_adjacent_potion_not_distant_ones(self):
        for adjacent in (True, False):
            engine, source, listener = self.board("wealth_potion", "lab_mouse", adjacent)
            engine._trigger_potion(0, source, engine.catalog.ingredients[source.def_id]["potion"])
            engine._run_script(1, listener, "lab_mouse")
            self.assertEqual(1 if adjacent else 0, listener.permanent_bonus)

    def test_removed_listener_cannot_react(self):
        engine, source, listener = self.board("water", "alchemy_slime")
        engine._remove(listener, "manual", 1)
        engine._transform(source, "ash")
        self.assertEqual(0, listener.permanent_bonus)

    def test_curse_vessel_is_adjacent_once_per_round_and_saved(self):
        engine, source, listener = self.board("blank_magic", "curse_vessel")
        engine.s.flags["guarantee_chance_next"] = True
        engine._chance(0.3, negative=True, source_index=0)
        engine.emit("negative_triggered", source_index=0)
        self.assertEqual(1, listener.permanent_bonus)
        engine.s.last_board = [{"uid": x.uid, "id": x.def_id, "coord": list(coord), "slot": i + 1, "value": 1, "present": True}
                               for i, (x, coord) in enumerate(zip(engine._board, engine._coords))]
        engine._sync_rng()
        restored = GameEngine().bind(GameState.from_dict(engine.s.to_dict()))
        restored.emit("negative_triggered", source_index=0)
        self.assertEqual(1, restored.s.ingredients[1].permanent_bonus)
        engine._round_events.clear()
        engine.emit("negative_triggered", source_index=0)
        self.assertEqual(2, listener.permanent_bonus)
        far, _, distant = self.board("blank_magic", "curse_vessel", False)
        far.emit("negative_triggered", source_index=0)
        self.assertEqual(0, distant.permanent_bonus)

    def test_alchemy_scrap_has_no_duplicate_or_distant_growth(self):
        for adjacent in (True, False):
            engine, source, listener = self.board("water", "alchemy_scrap", adjacent)
            engine._remove(source, "removed", 0)
            engine._run_script(1, listener, "alchemy_scrap")
            self.assertEqual(1 if adjacent else 0, listener.permanent_bonus)

    def test_minimal_doubles_one_real_event_not_duplicate_callbacks(self):
        engine, source, listener = self.board("water", "alchemy_slime", mode="minimal")
        engine._transform(source, "ash")
        engine._run_script(1, listener, "slime")
        self.assertEqual(2, listener.permanent_bonus)

    def test_manual_delete_notifies_only_listeners_adjacent_to_last_board_slot(self):
        engine, source, listener = self.board("water", "alchemy_scrap")
        engine.s.tokens["remove"] = 2
        offboard = engine.add_ingredient("water", emit=False)
        engine.remove(3)
        self.assertEqual(0, listener.permanent_bonus)
        engine.remove(1)
        self.assertEqual(1, listener.permanent_bonus)

    def test_prevented_negative_effect_does_not_grow_curse_vessel(self):
        engine, _, listener = self.board("blank_magic", "curse_vessel")
        engine.s.items.append("magic_filter")
        engine.s.flags["guarantee_chance_next"] = True
        engine._run_script(0, engine._board[0], "blank_magic")
        self.assertEqual(0, listener.permanent_bonus)

    def test_public_spin_matches_growth_descriptions(self):
        for seed, source_id, listener_id in ((0, "upgrade_magic", "alchemy_slime"), (7, "broken_flask", "glassmaker")):
            engine = self.fresh(seed)
            source = engine.add_ingredient(source_id, emit=False)
            if source_id == "upgrade_magic":
                source.age = 9
            listener = engine.add_ingredient(listener_id, emit=False)
            engine.spin()
            self.assertEqual(1, listener.permanent_bonus)


if __name__ == "__main__":
    unittest.main()
