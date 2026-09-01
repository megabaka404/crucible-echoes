from __future__ import annotations

from collections import defaultdict
import unittest

from crucible_echoes.engine import GameEngine
from crucible_echoes.model import GameState


class LatestContentTests(unittest.TestCase):
    def fresh(self, difficulty: int = 1) -> GameEngine:
        engine = GameEngine()
        engine.new_game(20260901, difficulty=difficulty)
        engine.s.ingredients.clear()
        engine.s.items.clear()
        engine.s.essences.clear()
        engine.s.consumed_essences.clear()
        engine.s.gold = 0
        engine._round_events = defaultdict(int)
        engine._round_event_values = defaultdict(int)
        return engine

    def put_on_board(self, engine: GameEngine, *def_ids: str, coords=None):
        instances = [engine.add_ingredient(def_id, emit=False) for def_id in def_ids]
        engine._board = instances
        engine._coords = list(coords or [(0, i) for i in range(len(instances))])
        engine._all_adjacent = False
        engine._panorama = False
        return instances

    def test_d7_order_bonus_is_single_existing_bonus_and_slag_interval_is_inherited(self) -> None:
        d7 = GameEngine(); d7.new_game(1, difficulty=7)
        self.assertEqual((700, 10), d7.current_order_for(10, 7, {}))
        self.assertEqual(20, GameEngine.slag_interval(7))
        d15 = GameEngine(); d15.new_game(1, difficulty=15)
        self.assertEqual((700, 10), d15.current_order_for(10, 15, {}))
        self.assertEqual(20, GameEngine.slag_interval(15))

    def test_pigments_same_different_and_global_tricolor_bonuses(self) -> None:
        engine = self.fresh()
        self.put_on_board(engine, "red_pigment", "red_pigment")
        self.assertEqual([2, 2], engine._apply_multipliers(engine._base_values()))

        engine = self.fresh()
        self.put_on_board(engine, "red_pigment", "blue_pigment")
        self.assertEqual([3, 3], engine._apply_multipliers(engine._base_values()))

        engine = self.fresh()
        self.put_on_board(
            engine,
            "red_pigment",
            "yellow_pigment",
            "blue_pigment",
            coords=[(0, 0), (0, 2), (2, 0)],
        )
        self.assertEqual([3, 3, 3], engine._apply_multipliers(engine._base_values()))

    def test_palette_bonus_is_once_per_pigment_even_with_multiple_same_neighbors(self) -> None:
        engine = self.fresh()
        engine.s.items.append("palette")
        self.put_on_board(
            engine,
            "red_pigment",
            "red_pigment",
            "red_pigment",
            coords=[(0, 0), (0, 1), (1, 0)],
        )
        # base 1 + pigment same-colour 1 + palette 1, regardless of neighbour count.
        self.assertEqual([3, 3, 3], engine._apply_multipliers(engine._base_values()))

    def test_palette_essence_persists_to_future_same_color_pigments(self) -> None:
        engine = self.fresh()
        red, _ = self.put_on_board(engine, "red_pigment", "red_pigment")
        engine.add_essence("palette_essence")
        engine._trigger_pigment_pair_essences()
        self.assertIn("palette_essence", engine.s.consumed_essences)
        self.assertEqual(2, engine.s.flags["global_permanent_bonuses"]["red_pigment"])
        engine._board = [red]
        engine._coords = [(0, 0)]
        self.assertEqual([3], engine._base_values())
        future = engine.add_ingredient("red_pigment", emit=False)
        engine._board = [future]
        self.assertEqual([3], engine._base_values())

    def test_pigment_box_is_one_all_or_nothing_choice_and_agent_exposes_definitions(self) -> None:
        engine = self.fresh()
        engine.add_item("pigment_box")
        self.assertEqual("bundle", engine.s.pending[0].kind)
        engine.s.tokens["roll"] = 1
        payload = engine.agent_payload("status")
        self.assertEqual(
            ["choose 1", "choose 2"],
            engine.agent_available_actions()[-2:],
        )
        self.assertNotIn("reroll", engine.agent_available_actions())
        self.assertEqual("bundle", payload["pending_choices"][0]["kind"])
        self.assertEqual("全部获得：红颜料、黄颜料、蓝颜料", payload["pending_choices"][0]["offers"][0]["definition"]["name"])
        engine.choose(1)
        self.assertEqual(1, sum(x.def_id == "red_pigment" for x in engine.s.ingredients))
        self.assertEqual(1, sum(x.def_id == "yellow_pigment" for x in engine.s.ingredients))
        self.assertEqual(1, sum(x.def_id == "blue_pigment" for x in engine.s.ingredients))

        reject = self.fresh()
        reject.add_item("pigment_box")
        reject.choose(2)
        self.assertFalse(any(x.def_id.endswith("_pigment") for x in reject.s.ingredients))

    def test_destroy_magic_is_positive_adjacent_destruction_and_not_magic_filter_negative_immunity(self) -> None:
        engine = self.fresh()
        destroyer, target = self.put_on_board(engine, "destroy_magic", "water")
        engine.s.items.append("magic_filter")
        chance_calls: list[float] = []
        engine._chance = lambda chance: chance_calls.append(chance) or True
        engine._run_script(0, destroyer, "destroy_magic")
        self.assertNotIn(target, engine.s.ingredients)
        self.assertEqual(1, engine.s.stats["event_counts"]["removed"])
        self.assertEqual([0.30], chance_calls)

        alone = self.fresh()
        destroyer, = self.put_on_board(alone, "destroy_magic")
        alone._chance = lambda chance: (_ for _ in ()).throw(AssertionError("no-neighbour must not roll"))
        alone._run_script(0, destroyer, "destroy_magic")
        self.assertIn(destroyer, alone.s.ingredients)

    def test_magic_and_summoner_use_new_spawn_probabilities_and_values(self) -> None:
        catalog = GameEngine().catalog
        self.assertEqual(3, catalog.ingredients["magic_magic"]["base"])
        self.assertEqual(0.30, catalog.ingredients["magic_magic"]["chance_spawn"]["chance"])
        self.assertEqual(3, catalog.ingredients["summoner"]["base"])
        self.assertEqual(0.50, catalog.ingredients["summoner"]["chance_spawn"]["chance"])

        for def_id in ("magic_magic", "summoner"):
            engine = self.fresh()
            source, = self.put_on_board(engine, def_id)
            chance_calls: list[float] = []
            engine._chance = lambda chance: chance_calls.append(chance) or True
            engine._run_active_effects()
            self.assertEqual(2, len(engine.s.ingredients))
            self.assertEqual([0.30 if def_id == "magic_magic" else 0.50], chance_calls)
            if def_id == "summoner":
                self.assertEqual("goblin", engine.s.ingredients[-1].def_id)

        summon = self.fresh()
        source, = self.put_on_board(summon, "summon_magic")
        summon._chance = lambda chance: True
        summon.roll_rarity = lambda kind, minimum=1, maximum=4: 3
        summon._run_active_effects()
        self.assertGreaterEqual(
            int(summon.catalog.ingredients[summon.s.ingredients[-1].def_id]["rarity"]),
            2,
        )

    def test_ban_toggles_only_component_owned_generation_and_bundle_still_works(self) -> None:
        engine = self.fresh()
        source, = self.put_on_board(engine, "summon_magic")
        engine.add_item("ban")
        self.assertTrue(engine.toggle_item("ban"))
        self.assertTrue(engine.status_payload()["ingredient_generation_disabled"])
        self.assertIn("toggle ban", engine.agent_available_actions())
        engine._chance = lambda chance: (_ for _ in ()).throw(AssertionError("disabled generator rolled"))
        engine._run_active_effects()
        self.assertEqual(1, len(engine.s.ingredients))
        self.assertFalse(engine.toggle_item("ban"))
        engine._chance = lambda chance: True
        engine._run_active_effects()
        self.assertEqual(2, len(engine.s.ingredients))

        engine.toggle_item("ban")
        engine.add_item("pigment_box")
        engine.choose(1)
        self.assertEqual(1, sum(x.def_id == "red_pigment" for x in engine.s.ingredients))

    def test_ban_essence_disables_current_and_future_generators_with_persistent_bonus(self) -> None:
        engine = self.fresh()
        current = engine.add_ingredient("summon_magic", emit=False)
        engine.add_essence("ban_essence")
        for _ in range(3):
            self.assertIsNotNone(engine._generated_ingredient("water"))
        self.assertTrue(engine.s.flags["ingredient_generation_permanently_disabled"])
        self.assertEqual(1, engine.s.flags["ingredient_generation_bonus"])
        future = engine.add_ingredient("magic_magic", emit=False)
        engine._board = [current, future]
        engine._coords = [(0, 0), (0, 1)]
        self.assertEqual([4, 4], engine._base_values())
        before = len(engine.s.ingredients)
        self.assertIsNone(engine._generated_ingredient("water"))
        self.assertEqual(before, len(engine.s.ingredients))

    def test_nested_chest_pays_ten_and_spawns_another_chest_without_recursion(self) -> None:
        engine = self.fresh()
        chest = engine.add_ingredient("nested_chest", emit=False)
        self.assertTrue(engine._remove(chest, "removed", None))
        self.assertEqual(10, engine.s.gold)
        self.assertEqual(1, len(engine.s.ingredients))
        spawned = engine.s.ingredients[0]
        self.assertIn("chest", engine.catalog.ingredients[spawned.def_id].get("tags", []))
        self.assertNotEqual("nested_chest", spawned.def_id)

        manual = self.fresh()
        manual.s.tokens["remove"] = 1
        manual.add_ingredient("nested_chest", emit=False)
        manual.remove(1)
        self.assertEqual(10, manual.s.gold)
        self.assertEqual(1, len(manual.s.ingredients))

    def test_scapegoat_reward_is_fifteen(self) -> None:
        engine = self.fresh()
        scapegoat, target = self.put_on_board(engine, "scapegoat", "water")
        self.assertFalse(engine._remove(target, "destroyed", 1))
        self.assertNotIn(scapegoat, engine.s.ingredients)
        self.assertEqual(15, engine.s.gold)

    def test_new_state_defaults_keep_old_saves_loadable(self) -> None:
        engine = self.fresh()
        data = engine.s.to_dict()
        data["flags"].pop("ingredient_generation_disabled", None)
        data["flags"].pop("ingredient_generation_permanently_disabled", None)
        data["flags"].pop("ingredient_generation_bonus", None)
        restored = GameEngine().bind(GameState.from_dict(data))
        self.assertFalse(restored.ingredient_generation_disabled())
        self.assertFalse(restored.s.flags["ingredient_generation_permanently_disabled"])
        self.assertEqual(0, restored.s.flags["ingredient_generation_bonus"])


if __name__ == "__main__":
    unittest.main()
