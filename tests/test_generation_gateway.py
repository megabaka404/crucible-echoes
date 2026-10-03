from copy import deepcopy
import unittest

from crucible_echoes.engine import GameEngine
from crucible_echoes.model import GameState
from crucible_echoes.simulation import HeuristicStrategy


class GenerationGatewayTests(unittest.TestCase):
    def fresh(self, *ids):
        engine = GameEngine()
        engine.new_game(42)
        engine.s.ingredients.clear()
        engine.s.gold = 0
        for name in ids:
            engine.add_ingredient(name, emit=False)
        engine._board = list(engine.s.ingredients)
        engine._coords = [(0, index) for index in range(len(ids))]
        engine._values = [engine.stable_ingredient_value(x) for x in engine._board]
        return engine

    def test_fixed_and_random_gateways_share_first_generation_reward(self):
        engine = self.fresh()
        engine.s.items.append("double_cauldron")
        rng = engine.r.state
        engine._generated_ingredient("water")
        engine._spawn_random(def_id="water", origin="ingredient")
        self.assertEqual(3, engine.s.gold)
        self.assertEqual(1, engine.s.stats["item_trigger_counts"]["double_cauldron"])
        self.assertEqual(2, engine.s.stats["event_counts"]["generated"])
        self.assertEqual(rng, engine.r.state)
        engine.s.spin += 1
        engine._round_events.clear()
        engine._generated_ingredient("water")
        self.assertEqual(6, engine.s.gold)

    def test_ordinary_gains_and_essence_generation_do_not_award_component_bonus(self):
        engine = self.fresh()
        engine.s.items.append("double_cauldron")
        engine.add_ingredient("water")
        engine._spawn_random(def_id="water", origin="essence")
        self.assertEqual(0, engine.s.gold)
        self.assertNotIn("double_cauldron", engine.s.stats.get("item_trigger_counts", {}))
        engine._generated_ingredient("water")
        self.assertEqual(3, engine.s.gold)

    def test_failed_or_banned_generation_does_not_consume_armed_effect(self):
        engine = self.fresh("unique")
        engine.s.items.append("double_cauldron")
        engine.s.flags["copy_next_generation"] = True
        rng = engine.r.state
        self.assertIsNone(engine._generated_ingredient("unique"))
        engine.s.flags["ingredient_generation_disabled"] = True
        self.assertIsNone(engine._generated_ingredient("water"))
        self.assertTrue(engine.s.flags["copy_next_generation"])
        self.assertEqual(0, engine.s.gold)
        self.assertEqual(0, engine.s.stats.get("event_counts", {}).get("generated", 0))
        self.assertEqual(rng, engine.r.state)

    def test_extra_generation_obeys_same_rarity_and_does_not_fake_copy(self):
        engine = self.fresh()
        engine.s.flags["copy_next_generation"] = True
        offered = []
        def choose(rows):
            offered.extend(name for name, _ in rows)
            return "magic_crucible"
        engine.r.weighted_choice = choose
        original = engine._generated_ingredient("unique")
        self.assertEqual("unique", original.def_id)
        self.assertNotIn("unique", offered)
        self.assertTrue(all(engine.catalog.ingredients[name]["rarity"] == 4 for name in offered))
        self.assertEqual(["unique", "magic_crucible"], [x.def_id for x in engine.s.ingredients])
        self.assertEqual(2, engine.s.stats["event_counts"]["generated"])
        self.assertEqual(0, engine.s.stats["event_counts"].get("copied", 0))
        self.assertNotIn("copy_next_generation", engine.s.flags)

    def test_extra_generation_never_falls_back_when_same_tier_exhausted(self):
        engine = self.fresh()
        for name in list(engine.catalog.ingredients):
            if name not in {"unique", "water"}:
                engine.catalog.ingredients.pop(name)
        engine.s.flags["copy_next_generation"] = True
        rng = engine.r.state
        engine._generated_ingredient("unique")
        self.assertEqual(["unique"], [x.def_id for x in engine.s.ingredients])
        self.assertEqual(1, engine.s.stats["event_counts"]["generated"])
        self.assertEqual(0, engine.s.stats["event_counts"].get("copied", 0))
        self.assertEqual(rng, engine.r.state)

    def test_essence_extra_is_allowed_after_immediate_ban_consumes_third_generation(self):
        engine = self.fresh()
        engine.add_essence("ban_essence")
        engine._generated_ingredient("water")
        engine._generated_ingredient("water")
        engine.s.flags["copy_next_generation"] = True
        engine.r.weighted_choice = lambda rows: "water"
        engine._generated_ingredient("water")
        self.assertTrue(engine.s.flags["ingredient_generation_permanently_disabled"])
        self.assertIn("ban_essence", engine.s.consumed_essences)
        self.assertEqual(4, len(engine.s.ingredients))
        self.assertEqual(4, engine.s.stats["event_counts"]["generated"])
        self.assertIsNone(engine._generated_ingredient("water"))

    def test_mineral_minimum_also_applies_to_fixed_component_generation(self):
        engine = self.fresh()
        engine.s.items.append("ore_sorting_table")
        created = engine._generated_ingredient("copper")
        self.assertGreaterEqual(engine.catalog.ingredients[created.def_id]["rarity"], 2)
        self.assertTrue(set(engine.catalog.ingredients[created.def_id]["tags"]) & {"stone", "ore", "metal"})

    def test_source_neighbors_grow_live_core_once_without_extra_rng(self):
        engine = self.fresh("copy_potion", "proliferation_core")
        rng = engine.r.state
        for _ in range(2):
            engine._generated_ingredient("water", source=0)
        self.assertEqual(1, engine._board[1].permanent_bonus)
        self.assertEqual(rng, engine.r.state)

    def test_true_copy_potion_keeps_copy_event_and_actual_recent_identity(self):
        engine = self.fresh("copy_potion", "copper")
        engine.s.items.extend(["ore_sorting_table", "double_cauldron"])
        engine._run_script(0, engine._board[0], "copy_potion")
        generated = engine.s.ingredients[-1]
        self.assertGreaterEqual(engine.catalog.ingredients[generated.def_id]["rarity"], 2)
        self.assertEqual(generated.def_id, engine.s.stats["recent_copied"])
        self.assertEqual(1, engine.s.stats["event_counts"]["copied"])
        self.assertEqual(3, engine.s.gold)

    def test_legacy_armed_flag_and_rng_trajectory_survive_reload(self):
        engine = self.fresh()
        engine.s.flags["copy_next_generation"] = True
        resumed = GameEngine().bind(GameState.from_dict(deepcopy(engine.s.to_dict())))
        engine._generated_ingredient("water")
        resumed._generated_ingredient("water")
        engine._sync_rng()
        resumed._sync_rng()
        self.assertEqual(engine.s.to_dict(), resumed.s.to_dict())
        self.assertEqual(engine.r.state, resumed.r.state)

    def test_flame_ash_generation_uses_shared_reward_and_source(self):
        engine = self.fresh("flame", "timber", "proliferation_core")
        engine._coords = [(0, 0), (0, 1), (1, 0)]
        engine.s.items.append("double_cauldron")
        rng = engine.r.state
        engine._flame(0)
        self.assertEqual(23, engine.s.gold)  # timber 2g * 10 plus first-generation 3g
        self.assertTrue(any(x.def_id == "ash" for x in engine.s.ingredients))
        self.assertEqual(1, engine._board[2].permanent_bonus)
        self.assertEqual(rng, engine.r.state)

    def test_nine_lives_return_counts_successful_generation_and_reward(self):
        engine = self.fresh("nine_lives_cat")
        engine.s.items.append("double_cauldron")
        original = engine._board[0]
        engine._remove(original, "manual", None)
        self.assertEqual(3, engine.s.gold)
        self.assertEqual(1, engine.s.stats["event_counts"]["generated"])
        self.assertEqual(1, len(engine.s.ingredients))
        self.assertEqual(7, engine.s.ingredients[0].flags["lives"])
        self.assertNotEqual(original.uid, engine.s.ingredients[0].uid)

    def test_declarative_removal_spawns_keep_source_for_neighbor_growth(self):
        for source, reason in (("conical_flask", "shattered"), ("nested_chest", "manual")):
            for indexed in (False, True):
                with self.subTest(source=source, indexed=indexed):
                    engine = self.fresh(source, "proliferation_core", "proliferation_core")
                    engine._coords = [(0, 0), (0, 1), (2, 3)]
                    engine.s.items.append("double_cauldron")
                    self.assertTrue(engine._remove(engine._board[0], reason, 0 if indexed else None))
                    self.assertEqual(1, engine._board[1].permanent_bonus)
                    self.assertEqual(0, engine._board[2].permanent_bonus)
                    self.assertEqual(1, engine.s.stats["event_counts"]["generated"])
                    self.assertEqual(1, engine.s.stats["item_trigger_counts"]["double_cauldron"])

    def test_removal_source_context_does_not_invent_offboard_adjacency(self):
        engine = self.fresh("proliferation_core")
        offboard = engine.add_ingredient("nested_chest", emit=False)
        engine._remove(offboard, "manual", None)
        self.assertEqual(0, engine._board[0].permanent_bonus)
        self.assertEqual(1, engine.s.stats["event_counts"]["generated"])

    def test_removal_spawn_source_is_reproducible_after_save_reload(self):
        engine = self.fresh("conical_flask", "proliferation_core")
        engine.s.last_board = [{"uid": x.uid, "id": x.def_id, "slot": index + 1,
                                "coord": list(engine._coords[index]), "value": engine._values[index]}
                               for index, x in enumerate(engine._board)]
        engine._sync_rng()
        resumed = GameEngine().bind(GameState.from_dict(deepcopy(engine.s.to_dict())))
        engine._remove(engine._board[0], "shattered", 0)
        resumed._remove(resumed._board[0], "shattered", 0)
        engine._sync_rng()
        resumed._sync_rng()
        self.assertEqual(engine.s.to_dict(), resumed.s.to_dict())
        self.assertEqual(engine.r.state, resumed.r.state)

    def test_recycle_payload_accepts_optional_source_context(self):
        for indexed in (False, True):
            with self.subTest(indexed=indexed):
                engine = self.fresh("recycle_potion", "proliferation_core")
                engine.s.items.append("double_cauldron")
                engine.s.removed_history = ["water"]
                if indexed:
                    engine._trigger_potion(0, engine._board[0], {"recycle": True})
                else:
                    engine._apply_potion_payload({"recycle": True}, "test")
                self.assertEqual(3, engine.s.gold)
                self.assertTrue(any(x.def_id == "water" for x in engine.s.ingredients))
                self.assertEqual(int(indexed), engine._board[1].permanent_bonus)

    def test_protection_dsl_keeps_legacy_policy_scoring_metadata(self):
        engine = self.fresh("test_tube")
        original = deepcopy(engine.catalog.items["advanced_tube_rack"])
        original.pop("protect_once")
        policy = HeuristicStrategy()
        current = policy.score_components(engine, "item", "advanced_tube_rack")
        engine.catalog.items["advanced_tube_rack"] = original
        self.assertEqual(current, policy.score_components(engine, "item", "advanced_tube_rack"))
