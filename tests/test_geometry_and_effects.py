from __future__ import annotations

import unittest

from crucible_echoes.engine import GameEngine
from crucible_echoes.geometry import adjacent_indices, board_coords


class GeometryAndEffectTests(unittest.TestCase):
    def test_consumption_reward_requires_actual_removal(self) -> None:
        engine = GameEngine(); engine.new_game(912)
        engine.s.ingredients.clear()
        eater = engine.add_ingredient("kitten", emit=False)
        food = engine.add_ingredient("coin", emit=False)
        guard = engine.add_ingredient("restraint", emit=False)
        engine._board = [eater, food, guard]
        engine._coords = [(0, 0), (0, 1), (0, 2)]
        before = engine.s.gold
        self.assertFalse(engine._consume_first(0, {"coin"}, reward=9))
        self.assertIn(food, engine.s.ingredients)
        self.assertEqual(before, engine.s.gold)
        self.assertTrue(engine._consume_first(0, {"coin"}, reward=9))
        self.assertNotIn(food, engine.s.ingredients)
        self.assertEqual(before + 9, engine.s.gold)
        self.assertFalse(engine._consume_first(0, {"coin"}, reward=9))
        self.assertEqual(before + 9, engine.s.gold)

    def test_eight_neighbor_adjacency_and_blueprint_cell(self) -> None:
        base = board_coords(False)
        center = base.index((1, 2))
        self.assertEqual(8, len(adjacent_indices(base, center)))
        expanded = board_coords(True)
        extra = expanded.index((-1, 2))
        neighbors = {expanded[i] for i in adjacent_indices(expanded, extra)}
        self.assertEqual({(0, 1), (0, 2), (0, 3)}, neighbors)

    def test_blueprint_is_once_per_run_and_expands_board(self) -> None:
        engine = GameEngine(); engine.new_game(1)
        result = engine.add_ingredient("blueprint")
        self.assertIsNone(result)
        self.assertTrue(engine.s.expanded)
        self.assertEqual(21, engine.status_payload()["board_capacity"])
        size = len(engine.s.ingredients)
        engine.add_ingredient("blueprint")
        self.assertEqual(size, len(engine.s.ingredients))

    def test_transformation_and_timed_removal(self) -> None:
        engine = GameEngine(); engine.new_game(3)
        engine.s.ingredients.clear()
        grass = engine.add_ingredient("grass_seed", emit=False); grass.age = 9
        ash = engine.add_ingredient("ash", emit=False); ash.age = 9
        engine.spin()
        ids = [x.def_id for x in engine.s.ingredients]
        self.assertIn("tall_grass", ids)
        self.assertNotIn("ash", ids)

    def test_pickaxe_removes_stone_and_generates_metal(self) -> None:
        engine = GameEngine(); engine.new_game(12)
        engine.s.ingredients.clear()
        engine.add_ingredient("pickaxe", emit=False)
        engine.add_ingredient("stone", emit=False)
        engine.spin()
        ids = [x.def_id for x in engine.s.ingredients]
        self.assertNotIn("stone", ids)
        self.assertTrue(any("metal" in engine.catalog.ingredients[x].get("tags", []) for x in ids))

    def test_permanent_bonus_changes_future_value(self) -> None:
        engine = GameEngine(); engine.new_game(8)
        engine.s.ingredients.clear()
        instance = engine.add_ingredient("water", emit=False, permanent_bonus=4)
        engine.spin()
        board_row = next(x for x in engine.s.last_board if x["uid"] == instance.uid)
        self.assertEqual(5, board_row["value"])


if __name__ == "__main__":
    unittest.main()
