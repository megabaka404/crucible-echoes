from __future__ import annotations

import copy
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from crucible_echoes.cli import main
from crucible_echoes.engine import FUN_MODES, GameEngine, GameError
from crucible_echoes.geometry import board_coords
from crucible_echoes.model import GameState


class FunModeTests(unittest.TestCase):
    def test_modes_are_valid_default_and_legacy_safe(self) -> None:
        normal = GameEngine()
        normal.new_game(7)
        self.assertEqual("none", normal.s.fun_mode)
        self.assertEqual(tuple(FUN_MODES), ("none", "giant", "rapid", "blind_box", "minimal", "mutation"))
        with self.assertRaises(GameError):
            GameEngine().new_game(7, fun_mode="giant,rapid")
        legacy = normal.s.to_dict()
        legacy.pop("fun_mode")
        for row in legacy["ingredients"]:
            row.pop("mutation_draw_count", None)
        restored = GameEngine().bind(GameState.from_dict(legacy))
        self.assertEqual("none", restored.s.fun_mode)
        self.assertTrue(all(x.mutation_draw_count == 0 for x in restored.s.ingredients))

    def test_cli_and_agent_accept_new_modes_and_report_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(0, main(["agent", "new", "--seed", "7", "--difficulty", "15", "--fun-mode", "mutation", "--save", str(path)]))
            payload = json.loads(output.getvalue().strip()[len("[STATE] "):])
            self.assertEqual("mutation", payload["fun_mode"])
            self.assertEqual("mutation", payload["state"]["fun_mode"])

    def test_giant_uses_forty_orthogonal_cells_and_doubles_gains(self) -> None:
        engine = GameEngine()
        engine.new_game(11, difficulty=11, fun_mode="giant")
        self.assertEqual(16, len(engine.s.ingredients))
        self.assertEqual(40, engine.board_capacity())
        self.assertEqual(40, len(board_coords(False, "giant")))
        engine._coords = board_coords(False, "giant")
        engine._board = engine.s.ingredients[:]
        center = engine._coords.index((2, 3))
        self.assertEqual(4, len(engine._neighbors(center)))
        engine.s.ingredients.clear()
        first = engine.gain_ingredient("water", emit=False)
        self.assertEqual(2, len(engine.s.ingredients))
        self.assertEqual(["water", "water"], [x.def_id for x in engine.s.ingredients])
        self.assertNotEqual(first.uid, engine.s.ingredients[1].uid)
        engine.gain_token("remove", 1, "测试")
        engine.gain_token("roll", 1, "测试")
        self.assertEqual(2, engine.s.tokens["remove"])
        self.assertEqual(1, engine.s.tokens["roll"])
        self.assertEqual(2850, engine.current_order_for(12, 11, {}, fun_mode="giant")[0])
        engine.s.endless_mode = True
        engine.s.endless_order = 1
        engine.s.endless_target = 1000
        self.assertEqual((2000, 10), engine.current_order())
        engine.s.endless_mode = False
        engine.s.peace_mode = True
        self.assertEqual((0, 7), engine.current_order())

    def test_rapid_deletes_after_settlement_and_queues_two_normal_rewards(self) -> None:
        engine = GameEngine()
        engine.new_game(19, fun_mode="rapid")
        before = len(engine.s.ingredients)
        engine.s.spins_left = 2
        engine.spin()
        self.assertEqual(before - 1, len(engine.s.ingredients))
        self.assertEqual(1, engine.s.stats["event_counts"].get("rapid_removed", 0))
        self.assertEqual(["spin", "spin"], [x.source for x in engine.s.pending if x.kind == "ingredient"])
        engine.gain_token("roll", 1, "测试")
        self.assertEqual(2, engine.s.tokens["roll"])

    def test_rapid_final_spin_still_has_two_normal_rewards(self) -> None:
        engine = GameEngine()
        engine.new_game(20, fun_mode="rapid")
        amount, _ = engine.current_order()
        engine.s.gold = amount
        engine.s.spins_left = 1
        engine.spin()
        self.assertEqual(2, sum(choice.source == "spin" for choice in engine.s.pending if choice.kind == "ingredient"))

    def test_blind_box_randomizes_each_gain_and_converts_roll_tokens(self) -> None:
        engine = GameEngine()
        engine.new_game(23, fun_mode="blind_box")
        self.assertEqual(5, len(engine.s.ingredients))
        engine.s.ingredients.clear()
        engine._draw_definition = lambda kind, rarity, **kwargs: "charcoal"  # type: ignore[method-assign]
        engine.gain_ingredient("water", emit=False)
        engine.gain_ingredient("water", emit=False)
        self.assertEqual(["charcoal", "charcoal"], [x.def_id for x in engine.s.ingredients])
        engine.gain_token("roll", 3, "测试")
        self.assertEqual(0, engine.s.tokens["roll"])
        self.assertEqual(3, engine.s.tokens["remove"] + engine.s.tokens["essence"])
        engine.s.order_index = 3
        self.assertEqual((120, 6), engine.current_order())
        engine.s.endless_mode = True
        engine.s.endless_target = 1000
        engine.s.endless_order = 1
        self.assertEqual((1000, 10), engine.current_order())

    def test_minimal_has_twelve_cells_value_bonus_growth_double_and_order_token(self) -> None:
        engine = GameEngine()
        engine.new_game(29, fun_mode="minimal")
        self.assertEqual(3, len(engine.s.ingredients))
        self.assertEqual(12, engine.board_capacity())
        self.assertEqual(12, len(board_coords(False, "minimal")))
        self.assertEqual([(0, 0), (0, 1), (0, 2), (0, 3)], board_coords(False, "minimal")[:4])
        engine._coords = board_coords(False, "minimal")
        engine._board = [engine.s.ingredients[0]] * 1
        self.assertEqual(2, len(engine._neighbors(0)))
        engine.s.ingredients.clear()
        one = engine.add_ingredient("water", emit=False)
        two = engine.add_ingredient("gem_ore", emit=False)
        four = engine.add_ingredient("philosopher_stone", emit=False)
        slag = engine.add_ingredient("slag", emit=False)
        engine._board = [one, two, four, slag]
        engine._coords = [(0, 0), (0, 1), (1, 0), (1, 1)]
        self.assertEqual([2, 3, 9, 0], engine._base_values())
        engine._permanent_bonus(one, 1)
        self.assertEqual(2, one.permanent_bonus)
        one.flags["temporary_value"] = 9
        self.assertEqual(4, engine._base_values()[0])
        amount, _ = engine.current_order()
        engine.s.gold = amount
        engine.s.spins_left = 1
        engine._board = []
        engine.spin()
        self.assertEqual(1, engine.s.tokens["remove"])

    def test_minimal_doubles_persistent_generator_bonus_from_ban_essence(self) -> None:
        engine = GameEngine()
        engine.new_game(30, fun_mode="minimal")
        engine.s.flags["ingredient_generation_permanently_disabled"] = True
        engine.s.flags["ingredient_generation_bonus"] = 1
        generator = engine.add_ingredient("magic_magic", emit=False)
        self.assertIsNotNone(generator)
        self.assertEqual(2, generator.permanent_bonus)
        engine._board = [generator]
        engine._coords = [(0, 0)]
        self.assertEqual(8, engine._base_values()[0])

        engine.s.flags["global_permanent_bonuses"]["magic_magic"] = 2
        self.assertEqual(12, engine._base_values()[0])

    def test_mutation_counts_per_instance_and_preserves_only_permanent_bonus(self) -> None:
        engine = GameEngine()
        engine.new_game(31, fun_mode="mutation")
        engine.s.ingredients.clear()
        first = engine.add_ingredient("water", emit=False, permanent_bonus=5)
        second = engine.add_ingredient("water", emit=False)
        slag = engine.add_ingredient("slag", emit=False)
        first.age = 9
        first.counter = 4
        first.stored_gold = 7
        first.flags["old_state"] = True
        engine._board = [first]
        engine._coords = [(0, 0)]
        for _ in range(4):
            engine._mark_mutation_draws()
        self.assertEqual(4, first.mutation_draw_count)
        self.assertEqual(0, second.mutation_draw_count)
        engine._mark_mutation_draws()
        before_added = engine.s.stats["event_counts"].get("ingredient_added", 0)
        engine._process_mutations()
        self.assertEqual(0, first.mutation_draw_count)
        self.assertNotEqual("water", first.def_id)
        self.assertEqual(5, first.permanent_bonus)
        self.assertEqual(0, first.age)
        self.assertEqual(0, first.counter)
        self.assertEqual(0, first.stored_gold)
        self.assertEqual({}, first.flags)
        self.assertEqual(before_added, engine.s.stats["event_counts"].get("ingredient_added", 0))
        engine._board = [second]
        engine._mark_mutation_draws()
        self.assertEqual(1, second.mutation_draw_count)
        engine._board = [slag]
        engine._mark_mutation_draws()
        self.assertEqual(0, slag.mutation_draw_count)

    def test_mutation_upgrade_and_repeat_are_deterministic_and_saveable(self) -> None:
        def prepare() -> GameEngine:
            result = GameEngine()
            result.new_game(37, fun_mode="mutation")
            result.s.ingredients.clear()
            instance = result.add_ingredient("gem_ore", emit=False)
            result._board = [instance]
            result._coords = [(0, 0)]
            instance.mutation_draw_count = 5
            result.r.random = lambda: 0.0  # type: ignore[method-assign]
            result._process_mutations()
            return result

        first = prepare()
        second = prepare()
        self.assertEqual(first.s.to_dict(), second.s.to_dict())
        self.assertEqual(3, first.catalog.ingredients[first.s.ingredients[0].def_id]["rarity"])
        self.assertEqual(0, first.s.ingredients[0].mutation_draw_count)
        snapshot = copy.deepcopy(first.s.to_dict())
        restored = GameEngine().bind(GameState.from_dict(snapshot))
        self.assertEqual(snapshot, restored.s.to_dict())


if __name__ == "__main__":
    unittest.main()
