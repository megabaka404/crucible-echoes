from __future__ import annotations

import json
import unittest

from crucible_echoes.engine import GameEngine
from crucible_echoes.model import GameState


class StatelessBoardTests(unittest.TestCase):
    def draw(self, *def_ids: str) -> GameEngine:
        engine = GameEngine()
        engine.new_game(4102)
        engine.s.ingredients.clear()
        engine.s.gold = 1000
        for def_id in def_ids:
            engine.add_ingredient(def_id, emit=False)
        engine.r.sample = lambda rows, count: list(rows)[:count]
        engine.spin()
        engine.s.pending.clear()
        return engine

    def reload(self, engine: GameEngine) -> GameEngine:
        data = json.loads(json.dumps(engine.s.to_dict()))
        return GameEngine().bind(GameState.from_dict(data))

    def test_duplicate_snakes_do_not_trigger_cyan_after_json_reload(self):
        live = self.draw("snake", "snake")
        resumed = self.reload(live)
        for engine in (live, resumed):
            before = engine.s.gold
            engine.add_essence("cyan_reagent_essence")
            engine.check_essences()
            self.assertEqual(before, engine.s.gold)
            self.assertIn("cyan_reagent_essence", engine.s.essences)
        self.assertEqual(live.s.to_dict(), resumed.s.to_dict())

    def test_unique_board_cyan_still_triggers_after_json_reload(self):
        live = self.draw("snake", "water")
        resumed = self.reload(live)
        for engine in (live, resumed):
            before = engine.s.gold
            engine.add_essence("cyan_reagent_essence")
            engine.check_essences()
            self.assertEqual(before + 40, engine.s.gold)
        self.assertEqual(live.s.to_dict(), resumed.s.to_dict())

    def test_real_adjacent_and_tag_essence_predicates_match_live_engine(self):
        for essence_id in ("blue_reagent_essence", "purple_reagent_essence"):
            with self.subTest(essence=essence_id):
                live = self.draw("water", "water", "water")
                resumed = self.reload(live)
                for engine in (live, resumed):
                    engine.add_essence(essence_id)
                    engine.check_essences()
                    self.assertIn(essence_id, engine.s.consumed_essences)
                self.assertEqual(live.s.to_dict(), resumed.s.to_dict())

    def test_offboard_pool_instances_do_not_satisfy_board_tag_condition(self):
        live = self.draw("water")
        live.add_ingredient("water", emit=False)
        live.add_ingredient("water", emit=False)
        resumed = self.reload(live)
        self.assertEqual(1, len(resumed._board))
        self.assertEqual(3, len(resumed.s.ingredients))
        for engine in (live, resumed):
            engine.add_essence("blue_reagent_essence")
            engine.check_essences()
            self.assertIn("blue_reagent_essence", engine.s.essences)
        self.assertEqual(live.s.to_dict(), resumed.s.to_dict())

    def test_departed_slot_is_retained_and_not_counted_as_live(self):
        live = self.draw("water", "water", "water")
        live._remove(live._board[1], "manual", None)
        live._sync_rng()
        resumed = self.reload(live)
        self.assertEqual(3, len(resumed._board))
        self.assertEqual([(0, 0), (0, 1), (0, 2)], resumed._coords)
        self.assertFalse(resumed._present(resumed._board[1]))
        self.assertFalse(resumed._has_adjacent_same(2))
        self.assertFalse(live._has_adjacent_same(2))
        for engine in (live, resumed):
            engine.add_essence("blue_reagent_essence")
            engine.add_essence("purple_reagent_essence")
            engine.check_essences()
            self.assertIn("blue_reagent_essence", engine.s.essences)
            self.assertIn("purple_reagent_essence", engine.s.essences)
        self.assertEqual(live.s.to_dict(), resumed.s.to_dict())

    def test_departed_duplicate_does_not_block_unique_board_condition(self):
        live = self.draw("snake", "snake")
        live._remove(live._board[1], "manual", None)
        live._sync_rng()
        resumed = self.reload(live)
        for engine in (live, resumed):
            before = engine.s.gold
            engine.add_essence("cyan_reagent_essence")
            engine.check_essences()
            self.assertEqual(before + 40, engine.s.gold)
        self.assertEqual(live.s.to_dict(), resumed.s.to_dict())

    def test_hydrated_live_slots_use_actual_pool_instances_and_saved_values(self):
        live = self.draw("water", "snake")
        resumed = self.reload(live)
        self.assertIs(resumed._board[0], resumed.s.ingredients[0])
        self.assertIs(resumed._board[1], resumed.s.ingredients[1])
        self.assertEqual([row["value"] for row in live.s.last_board], resumed._values)
        self.assertEqual(live.s.to_dict(), resumed.s.to_dict())
        self.assertEqual(live.r.state, resumed.r.state)

    def test_no_board_legacy_save_does_not_vacuously_award_cyan(self):
        engine = GameEngine()
        engine.new_game(12)
        old_data = engine.s.to_dict()
        old_data.pop("last_board")
        old_data["spin"] = 5
        resumed = GameEngine().bind(GameState.from_dict(old_data))
        before_rng = resumed.r.state
        before_gold = resumed.s.gold
        resumed.add_essence("cyan_reagent_essence")
        resumed.check_essences()
        self.assertEqual([], resumed._board)
        self.assertEqual(before_gold, resumed.s.gold)
        self.assertEqual(before_rng, resumed.r.state)
        self.assertIn("cyan_reagent_essence", resumed.s.essences)

    def test_legacy_saved_slots_without_coords_use_their_original_slot(self):
        live = self.draw("water", "snake")
        old_data = live.s.to_dict()
        old_data["stats"].pop("last_board_topology")
        for row in old_data["last_board"]:
            row.pop("coord")
            row.pop("present")
        resumed = GameEngine().bind(GameState.from_dict(old_data))
        self.assertEqual([(0, 0), (0, 1)], resumed._coords)
        self.assertTrue(all(resumed._present(instance) for instance in resumed._board))

    def test_one_spin_all_adjacent_topology_survives_flag_decay_and_reload(self):
        engine = GameEngine()
        engine.new_game(55)
        engine.s.flags["all_adjacent_spins"] = 1
        engine.spin()
        self.assertEqual(0, engine.s.flags["all_adjacent_spins"])
        self.assertTrue(engine._all_adjacent)
        resumed = self.reload(engine)
        self.assertTrue(resumed._all_adjacent)
        self.assertEqual(engine._neighbors(0), resumed._neighbors(0))
        self.assertEqual(engine.s.to_dict(), resumed.s.to_dict())

    def test_legacy_periodic_topology_is_recovered_without_new_fields(self):
        engine = GameEngine()
        engine.new_game(57)
        engine.s.spin = 2
        engine.s.items.extend(("panorama_mirror", "global_reaction_field"))
        engine.spin()
        old_data = engine.s.to_dict()
        old_data["stats"].pop("last_board_topology")
        resumed = GameEngine().bind(GameState.from_dict(old_data))
        self.assertTrue(resumed._panorama)
        self.assertTrue(resumed._all_adjacent)
        self.assertEqual(engine._neighbors(0), resumed._neighbors(0))

    def test_reused_engine_new_game_clears_previous_round_snapshot(self):
        engine = self.draw("snake", "snake")
        engine._all_adjacent = True
        engine._panorama = True
        engine.new_game(58)
        self.assertEqual([], engine._board)
        self.assertEqual([], engine._coords)
        self.assertEqual([], engine._values)
        self.assertFalse(engine._all_adjacent)
        self.assertFalse(engine._panorama)

    def test_item_round_predicates_ignore_departed_tags_and_zero_values(self):
        engine = self.draw("water", "water", "snake")
        engine._remove(engine._board[2], "manual", None)
        engine.s.items.append("orange_reagent")
        before = engine.s.gold
        engine._run_round_conditions(2)
        self.assertEqual(before, engine.s.gold)
        engine.s.items.append("blue_reagent")
        engine._run_round_conditions(2)
        self.assertEqual(before, engine.s.gold)


if __name__ == "__main__":
    unittest.main()
