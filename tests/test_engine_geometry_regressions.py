from __future__ import annotations

import unittest

from crucible_echoes.engine import GameEngine
from crucible_echoes.geometry import board_coords
from crucible_echoes.rng import DeterministicRNG


class EngineGeometryRegressionTests(unittest.TestCase):
    def engine(self, mode: str = "none") -> GameEngine:
        engine = GameEngine()
        engine.new_game(487, fun_mode=mode)
        engine.s.ingredients.clear()
        return engine

    def test_all_board_modes_use_their_actual_edges_and_four_corners(self):
        for mode, last_row, last_col in (("none", 3, 4), ("giant", 4, 7), ("minimal", 2, 3)):
            with self.subTest(mode=mode):
                engine = self.engine(mode)
                before_rng = engine.r.state
                for row, col in board_coords(False, mode):
                    coord = (row, col)
                    self.assertEqual(row in (0, last_row) or col in (0, last_col), engine._is_edge(coord), coord)
                    self.assertEqual(row in (0, last_row) and col in (0, last_col), engine._is_corner(coord), coord)
                self.assertEqual(before_rng, engine.r.state)

    def test_expansion_remains_an_edge_without_replacing_any_corner(self):
        for mode, corners in (("none", {(0, 0), (0, 4), (3, 0), (3, 4)}),
                              ("giant", {(0, 0), (0, 7), (4, 0), (4, 7)}),
                              ("minimal", {(0, 0), (0, 3), (2, 0), (2, 3)})):
            with self.subTest(mode=mode):
                engine = self.engine(mode)
                engine.s.expanded = True
                coords = board_coords(True, mode)
                self.assertTrue(engine._is_edge(coords[0]))
                self.assertFalse(engine._is_corner(coords[0]))
                self.assertEqual(corners, {coord for coord in coords if engine._is_corner(coord)})

    def test_minimal_spin_gives_all_four_corner_jellyfish_their_bonus(self):
        engine = self.engine("minimal")
        for _ in board_coords(False, "minimal"):
            engine.add_ingredient("jellyfish", emit=False)
        engine.spin()
        corners = {(0, 0), (0, 3), (2, 0), (2, 3)}
        for row in engine.s.last_board:
            self.assertEqual(6 if tuple(row["coord"]) in corners else 2, row["value"])
        self.assertEqual(40, engine.s.stats["last_income"])

    def test_minimal_spin_gives_bottom_and_right_edge_moss_their_bonus(self):
        engine = self.engine("minimal")
        for _ in board_coords(False, "minimal"):
            engine.add_ingredient("moss", emit=False)
        engine.spin()
        for cell in engine.s.last_board:
            row, col = cell["coord"]
            self.assertEqual(3 if row in (0, 2) or col in (0, 3) else 2, cell["value"])
        self.assertEqual(34, engine.s.stats["last_income"])

    def test_minimal_panorama_uses_actual_bottom_right_corner(self):
        engine = self.engine("minimal")
        engine._coords = board_coords(False, "minimal")
        engine._board = [engine.add_ingredient("water", emit=False) for _ in engine._coords]
        engine._panorama = True
        corner_index = engine._coords.index((2, 3))
        self.assertEqual(11, len(engine._neighbors(corner_index)))
        middle_index = engine._coords.index((1, 1))
        self.assertTrue({engine._coords.index(coord) for coord in ((0, 0), (0, 3), (2, 0), (2, 3))}.issubset(engine._neighbors(middle_index)))

    def test_normal_and_giant_position_values_are_unchanged(self):
        for mode, coord, expected in (("none", (3, 4), 5), ("giant", (4, 7), 5), ("none", (2, 3), 1)):
            with self.subTest(mode=mode, coord=coord):
                engine = self.engine(mode)
                jellyfish = engine.add_ingredient("jellyfish", emit=False)
                engine._board = [jellyfish]
                engine._coords = [coord]
                self.assertEqual([expected], engine._base_values())

    def test_mirror_copies_actual_opposite_cell_in_every_board_mode(self):
        for mode, opposite in (("none", (3, 4)), ("giant", (4, 7)), ("minimal", (2, 3))):
            with self.subTest(mode=mode):
                engine = self.engine(mode)
                mirror = engine.add_ingredient("mirror", emit=False)
                target = engine.add_ingredient("water", emit=False, permanent_bonus=6)
                engine._board = [mirror, target]
                engine._coords = [(0, 0), opposite]
                values = engine._base_values()
                before_rng = engine.r.state
                settled = engine._apply_multipliers(values)
                self.assertEqual(values[0] + values[1], settled[0])
                self.assertEqual(before_rng, engine.r.state)

    def test_giant_prism_reaches_sixth_and_seventh_cells(self):
        engine = self.engine("giant")
        prism = engine.add_ingredient("copper_prism", emit=False)
        six = engine.add_ingredient("water", emit=False)
        seven = engine.add_ingredient("water", emit=False)
        engine._board = [prism, six, seven]
        engine._coords = [(0, 0), (0, 6), (0, 7)]
        engine.r.choice = lambda _directions: (0, 1)
        self.assertEqual([0, 2, 2], engine._apply_multipliers([0, 1, 1]))

    def test_normal_prism_retains_one_random_direction_draw(self):
        engine = self.engine()
        prism = engine.add_ingredient("copper_prism", emit=False)
        target = engine.add_ingredient("water", emit=False)
        engine._board = [prism, target]
        engine._coords = [(0, 0), (0, 4)]
        expected_rng = DeterministicRNG(engine.r.state)
        expected_rng.choice([(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)])
        engine._apply_multipliers([0, 1])
        self.assertEqual(expected_rng.state, engine.r.state)

    def test_stable_value_matches_settlement_prefix_without_side_effects(self):
        for mode in ("none", "giant", "minimal"):
            with self.subTest(mode=mode):
                engine = self.engine(mode)
                target = engine.add_ingredient("red_pigment", emit=False, permanent_bonus=3)
                engine.s.flags["global_permanent_bonuses"]["red_pigment"] = 2
                engine._board = [target]
                engine._coords = [(1, 1)]
                before = engine.s.to_dict()
                before_rng = engine.r.state
                stable = engine.stable_ingredient_value(target)
                definition = engine.catalog.ingredients["red_pigment"]
                expected_global = 4 + int(definition["rarity"]) if mode == "minimal" else 2
                self.assertEqual(int(definition["base"]) + 3 + expected_global, stable)
                self.assertEqual(before, engine.s.to_dict())
                self.assertEqual(before_rng, engine.r.state)
                self.assertEqual([stable], engine._base_values())

    def test_stable_value_respects_slag_difficulty_override(self):
        engine = self.engine("minimal")
        engine.s.difficulty = 12
        slag = engine.add_ingredient("slag", emit=False, permanent_bonus=9)
        engine.s.flags["global_permanent_bonuses"]["slag"] = 7
        self.assertEqual(0, engine.stable_ingredient_value(slag))
        engine._board = [slag]
        engine._coords = [(0, 0)]
        self.assertEqual([0], engine._base_values())

    def test_stable_value_accounts_for_materialized_and_legacy_generator_bonus(self):
        engine = self.engine("minimal")
        engine.s.flags["ingredient_generation_permanently_disabled"] = True
        engine.s.flags["ingredient_generation_bonus"] = 1
        generator = engine.add_ingredient("summon_magic", emit=False)
        self.assertEqual(2, generator.permanent_bonus)
        self.assertEqual(7, engine.stable_ingredient_value(generator))
        generator.flags.pop("ingredient_generation_bonus_applied")
        generator.permanent_bonus = 0
        self.assertEqual(7, engine.stable_ingredient_value(generator))

    def test_removal_magic_slot_zero_is_reflected_in_paid_income(self):
        engine = self.engine()
        engine.add_ingredient("removal_magic", emit=False)
        engine.add_ingredient("water", emit=False, permanent_bonus=10)
        engine.r.sample = lambda rows, count: list(rows)[:count]
        engine._chance = lambda _chance, **_kwargs: True
        before = engine.s.gold
        self.assertEqual(3, engine.spin())
        self.assertEqual([3, 0], [row["value"] for row in engine.s.last_board])
        self.assertEqual(before + 3, engine.s.gold)

    def test_failed_gambler_zero_is_reflected_in_paid_income(self):
        engine = self.engine()
        engine.add_ingredient("gambler", emit=False, permanent_bonus=7)
        engine.add_ingredient("coin", emit=False)
        engine.r.sample = lambda rows, count: list(rows)[:count]
        engine._chance = lambda _chance, **_kwargs: False
        before = engine.s.gold
        income = engine.spin()
        self.assertEqual(0, engine.s.last_board[0]["value"])
        self.assertEqual(sum(row["value"] for row in engine.s.last_board), income)
        self.assertEqual(before + income, engine.s.gold)

    def test_value_replacement_preserves_doubled_item_income(self):
        engine = self.engine()
        engine.add_ingredient("removal_magic", emit=False)
        engine.add_ingredient("water", emit=False, permanent_bonus=10)
        engine.s.items.append("vault")
        engine.s.flags["double_next_income"] = True
        engine.r.sample = lambda rows, count: list(rows)[:count]
        engine._chance = lambda _chance, **_kwargs: True
        before = engine.s.gold
        item_income = int(engine.catalog.items["vault"]["per_spin_gold"])
        self.assertEqual((3 + item_income) * 2, engine.spin())
        self.assertEqual(before + (3 + item_income) * 2, engine.s.gold)

    def test_magic_filter_preserves_target_income_when_negative_effect_blocked(self):
        engine = self.engine()
        engine.add_ingredient("removal_magic", emit=False)
        engine.add_ingredient("water", emit=False, permanent_bonus=10)
        engine.s.items.append("magic_filter")
        engine.r.sample = lambda rows, count: list(rows)[:count]
        engine.r.random = lambda: 0.0
        before = engine.s.gold
        self.assertEqual(14, engine.spin())
        self.assertEqual([3, 11], [row["value"] for row in engine.s.last_board])
        self.assertEqual(before + 14, engine.s.gold)
        self.assertEqual(1, engine.s.stats["event_counts"]["negative_prevented"])

    def test_negative_component_income_cannot_make_wallet_negative(self):
        engine = self.engine()
        engine.add_ingredient("scrap_iron", emit=False, permanent_bonus=-5)
        engine.s.gold = 1
        self.assertEqual(-4, engine.spin())
        self.assertEqual(-4, engine.s.stats["last_income"])
        self.assertEqual(0, engine.s.gold)
        self.assertEqual("playing", engine.s.status)

    def test_repeated_rust_decay_remains_safe_at_the_spin_payout_boundary(self):
        engine = self.engine()
        rust = engine.add_ingredient("rust", emit=False)
        metal = engine.add_ingredient("scrap_iron", emit=False)
        engine._board = [rust, metal]
        engine._coords = [(0, 0), (0, 1)]
        engine._chance = lambda _chance, **_kwargs: True
        for _ in range(4):
            engine._run_script(0, rust, "rust")
        self.assertEqual(-4, metal.permanent_bonus)
        engine.s.ingredients = [metal]
        engine.s.gold = 0
        self.assertEqual(-3, engine.spin())
        self.assertEqual(0, engine.s.gold)


if __name__ == "__main__":
    unittest.main()
