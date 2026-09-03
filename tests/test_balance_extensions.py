from __future__ import annotations

from collections import defaultdict
import unittest

from crucible_echoes.engine import GameEngine
from crucible_echoes.model import GameState, PendingChoice


class BalanceExtensionTests(unittest.TestCase):
    def fresh(self) -> GameEngine:
        engine = GameEngine()
        engine.new_game(20260819)
        engine.s.ingredients.clear()
        engine.s.items.clear()
        engine.s.essences.clear()
        engine.s.consumed_essences.clear()
        engine.s.gold = 0
        engine._round_events = defaultdict(int)
        engine._round_event_values = defaultdict(int)
        return engine

    def test_ore_sorting_table_guarantees_every_mineral(self) -> None:
        engine = self.fresh()
        engine.s.items.append("ore_sorting_table")
        first = engine._spawn_random(tag="stone", rarity=1)
        second = engine._spawn_random(tag="stone", rarity=1)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertGreaterEqual(int(engine.catalog.ingredients[first.def_id]["rarity"]), 2)
        self.assertGreaterEqual(int(engine.catalog.ingredients[second.def_id]["rarity"]), 2)

    def test_ore_sorting_table_and_vein_minimum_do_not_stack(self) -> None:
        table = self.fresh()
        table.s.items.append("ore_sorting_table")
        calls: list[float] = []
        table.r.random = lambda: calls.append(0.0) or 0.0
        created = table._spawn_random(
            tag="stone",
            rarity=1,
            minimum_rarity_chance={"chance": 1.0, "minimum": 2},
        )
        self.assertIsNotNone(created)
        self.assertGreaterEqual(int(table.catalog.ingredients[created.def_id]["rarity"]), 2)
        # The redundant vein-style chance is not rolled after the table already
        # guarantees 2; only the definition selection consumes random().
        self.assertEqual(1, len(calls))

        ordinary = self.fresh()
        ordinary.r.random = lambda: 0.99
        low = ordinary._spawn_random(
            tag="stone",
            rarity=1,
            minimum_rarity_chance={"chance": 0.0, "minimum": 2},
        )
        high = ordinary._spawn_random(
            tag="stone",
            rarity=1,
            minimum_rarity_chance={"chance": 1.0, "minimum": 2},
        )
        self.assertEqual(1, int(ordinary.catalog.ingredients[low.def_id]["rarity"]))
        self.assertGreaterEqual(int(ordinary.catalog.ingredients[high.def_id]["rarity"]), 2)

    def test_vein_periodic_spawn_is_every_eight_and_always_at_least_two(self) -> None:
        periodic = self.fresh().catalog.ingredients["vein"]["periodic_spawn"]
        self.assertEqual(8, periodic["every"])
        self.assertEqual(2, periodic["minimum_rarity"])
        self.assertNotIn("minimum_rarity_chance", periodic)
        engine = self.fresh()
        vein = engine.add_ingredient("vein", emit=False)
        vein.counter = 8
        engine._board = [vein]
        engine._coords = [(0, 0)]
        engine._values = [0]
        engine.r.random = lambda: 0.99
        engine._run_active_effects()
        generated = [x for x in engine.s.ingredients if x.uid != vein.uid]
        self.assertEqual(1, len(generated))
        self.assertGreaterEqual(int(engine.catalog.ingredients[generated[0].def_id]["rarity"]), 2)
        self.assertEqual(1, vein.permanent_bonus)
        self.assertEqual(0, vein.counter)

    def _run_summon_attempt(self, engine: GameEngine, success: bool) -> None:
        engine._chance = lambda _chance, result=success: result
        source = next(x for x in engine.s.ingredients if x.def_id == "summon_magic")
        engine._board = [source]
        engine._coords = [(0, 0)]
        engine._values = [0]
        before = len(engine.s.ingredients)
        engine._run_active_effects()
        if success:
            self.assertEqual(before + 1, len(engine.s.ingredients))
        else:
            self.assertEqual(before, len(engine.s.ingredients))

    def test_summon_magic_guarantees_fourth_success_then_resets(self) -> None:
        engine = self.fresh()
        engine.add_ingredient("summon_magic", emit=False)
        for _ in range(3):
            self._run_summon_attempt(engine, True)
        self.assertEqual(3, engine.s.stats["spawn_counters"]["summon_magic"])
        self._run_summon_attempt(engine, True)
        fourth = engine.s.ingredients[-1]
        self.assertGreaterEqual(int(engine.catalog.ingredients[fourth.def_id]["rarity"]), 2)
        self.assertEqual(0, engine.s.stats["spawn_counters"]["summon_magic"])
        self._run_summon_attempt(engine, True)
        # After the guaranteed fourth success, normal summons use the full
        # rarity table again; they are no longer forced to rarity 1.
        self.assertIn(int(engine.catalog.ingredients[engine.s.ingredients[-1].def_id]["rarity"]), {1, 2, 3, 4})
        self.assertEqual(1, engine.s.stats["spawn_counters"]["summon_magic"])

    def test_summon_failure_does_not_advance_counter(self) -> None:
        engine = self.fresh()
        engine.add_ingredient("summon_magic", emit=False)
        self._run_summon_attempt(engine, False)
        self.assertEqual(0, engine.s.stats["spawn_counters"].get("summon_magic", 0))
        self._run_summon_attempt(engine, True)
        self.assertEqual(1, engine.s.stats["spawn_counters"]["summon_magic"])

    def test_impossible_container_essence_rewards_75_and_removes_five_common(self) -> None:
        engine = self.fresh()
        for _ in range(35):
            engine.add_ingredient("water", emit=False)
        engine.add_essence("impossible_container_essence")
        engine.check_essences()
        self.assertEqual(75, engine.s.gold)
        self.assertEqual(30, len(engine.s.ingredients))
        self.assertIn("impossible_container_essence", engine.s.consumed_essences)
        self.assertGreaterEqual(engine.s.stats["event_counts"]["removed"], 5)
        self.assertTrue(all(engine.catalog.ingredients[x.def_id]["rarity"] == 1 for x in engine.s.ingredients))

    def test_item_balance_updates_are_data_driven(self) -> None:
        catalog = GameEngine().catalog
        self.assertEqual(6, catalog.items["cat_litter"]["event_bonus"]["removed_tag:cat"])
        self.assertEqual(
            {"every": 2, "amount": 3},
            catalog.items["brown_reagent"]["event_bonus_every"]["ingredient_added"],
        )
        self.assertEqual(2.77, catalog.items["golden_lucky_core"]["rarity_multiplier"])
        self.assertEqual(1.15, catalog.items["lucky_charm"]["rarity_multiplier"])
        self.assertEqual(1.30, catalog.items["lucky_compass"]["candidate_rarity_weight"])
        self.assertEqual(4, catalog.items["double_ledger"]["rarity"])
        self.assertEqual(4, catalog.items["tool_belt"]["rarity"])
        self.assertEqual(4, catalog.items["reagent_rack"]["rarity"])
        self.assertEqual(4, catalog.items["blue_reagent"]["round_condition"]["gold"])
        self.assertEqual(8, catalog.items["sorting_bin"]["event_bonus"]["removed_ids:ash,rust,alchemy_scrap"])
        self.assertEqual(1.0, catalog.items["magic_filter"]["negative_cancel_chance"])
        self.assertEqual(3, catalog.items["anomaly_recorder"]["rarity"])
        self.assertEqual(3, catalog.items["reaction_echo"]["rarity"])
        self.assertIn("正的永久加值", catalog.items["reaction_echo"]["description"])
        self.assertEqual(4, catalog.items["spare_key"]["event_bonus"]["opened"])
        self.assertIn("4g", catalog.items["spare_key"]["description"])
        self.assertEqual(4, catalog.items["small_safe"]["event_bonus"]["token"])
        self.assertIn("4g", catalog.items["small_safe"]["description"])
        self.assertEqual(3, catalog.ingredients["magic_magic"]["base"])
        self.assertEqual(2, catalog.ingredients["proliferation_core"]["base"])
        self.assertEqual(20, catalog.ingredients["mercenary"]["reward_gold"])
        self.assertEqual(10, catalog.ingredients["nested_chest"]["on_removed"]["gold"])
        self.assertIn("10g", catalog.ingredients["nested_chest"]["description"])
        self.assertEqual(7, catalog.items["animal_registry"].get("first_animal_gold", 7))
        self.assertEqual(8, catalog.items["impossible_container"].get("per_spin_cap", 8))
        self.assertEqual([1, 1, 2, 2], catalog.items["large_reactor"]["on_acquire"]["fixed_ingredient_choices"])
        self.assertEqual(0.42, catalog.ingredients["paper"]["growth_chance"])
        self.assertIn("42%", catalog.ingredients["paper"]["description"])
        self.assertEqual({"minimum": 3, "count": 2}, catalog.ingredients["lucky_potion"]["potion"]["choice_minimum"])

    def test_reaction_echo_pays_once_for_positive_permanent_bonus(self) -> None:
        engine = self.fresh()
        source = engine.add_ingredient("water", emit=False)
        engine.s.items.append("reaction_echo")
        self.assertIsNotNone(source)
        engine._permanent_bonus(source, 3)
        self.assertEqual(3, engine.s.gold)
        engine._permanent_bonus(source, 2)
        self.assertEqual(3, engine.s.gold)
        self.assertEqual(5, source.permanent_bonus)

    def test_reaction_echo_ignores_negative_bonus_without_losing_gold(self) -> None:
        engine = self.fresh()
        source = engine.add_ingredient("water", emit=False)
        engine.s.items.append("reaction_echo")
        self.assertIsNotNone(source)
        engine._permanent_bonus(source, -1)
        self.assertEqual(0, engine.s.gold)
        self.assertNotIn("reaction_echo", engine._round_events)

    def test_reaction_echo_resets_on_new_round(self) -> None:
        engine = self.fresh()
        source = engine.add_ingredient("water", emit=False)
        engine.s.items.append("reaction_echo")
        self.assertIsNotNone(source)
        engine._permanent_bonus(source, 1)
        self.assertEqual(1, engine.s.gold)
        engine._round_events = defaultdict(int)
        engine._permanent_bonus(source, 2)
        self.assertEqual(3, engine.s.gold)

    def test_brown_reagent_pays_three_for_each_pair_of_new_ingredients(self) -> None:
        engine = self.fresh()
        engine.s.items.append("brown_reagent")
        engine.add_ingredient("water")
        self.assertEqual(0, engine.s.gold)
        engine.add_ingredient("charcoal")
        self.assertEqual(3, engine.s.gold)
        engine.add_ingredient("cauldron")
        self.assertEqual(3, engine.s.gold)
        engine.add_ingredient("test_tube")
        self.assertEqual(6, engine.s.gold)
        self.assertEqual(4, engine.s.stats["item_event_counts"]["brown_reagent:ingredient_added"])

    def test_cat_litter_pays_six_for_each_removed_cat(self) -> None:
        engine = self.fresh()
        engine.s.items.append("cat_litter")
        cat = engine.add_ingredient("kitten", emit=False)
        self.assertIsNotNone(cat)
        self.assertTrue(engine._remove(cat, "removed", None))
        self.assertEqual(6, engine.s.gold)

    def test_mercenary_uses_data_driven_twenty_gold_reward(self) -> None:
        engine = self.fresh()
        mercenary = engine.add_ingredient("mercenary", emit=False)
        monster = engine.add_ingredient("goblin", emit=False)
        self.assertIsNotNone(mercenary)
        self.assertIsNotNone(monster)
        engine._board = [mercenary, monster]
        engine._coords = [(0, 0), (0, 1)]
        engine._run_script(0, mercenary, "mercenary")
        self.assertEqual(20, engine.s.gold)
        self.assertNotIn(monster.uid, {item.uid for item in engine.s.ingredients})

    def test_nested_chest_pays_ten_when_removed(self) -> None:
        engine = self.fresh()
        chest = engine.add_ingredient("nested_chest", emit=False)
        self.assertIsNotNone(chest)
        self.assertTrue(engine._remove(chest, "removed", None))
        self.assertEqual(10, engine.s.gold)

    def test_spare_key_pays_four_for_each_opened_chest(self) -> None:
        engine = self.fresh()
        engine.s.items.append("spare_key")
        chest = engine.add_ingredient("wood_chest", emit=False)
        self.assertIsNotNone(chest)
        self.assertTrue(engine._remove(chest, "opened", None))
        self.assertEqual(14, engine.s.gold)

    def test_sorting_bin_pays_eight_for_target_waste_and_emits_event(self) -> None:
        engine = self.fresh()
        engine.s.items.append("sorting_bin")
        waste = engine.add_ingredient("ash", emit=False)
        self.assertIsNotNone(waste)
        self.assertTrue(engine._remove(waste, "removed", None))
        self.assertEqual(8, engine.s.gold)
        self.assertEqual(1, engine.s.stats["event_counts"]["removed_ids:ash,rust,alchemy_scrap"])

    def test_impossible_container_starts_after_pool_exceeds_thirty(self) -> None:
        for count, expected in ((30, 0), (31, 1), (38, 8), (40, 8), (50, 8)):
            engine = self.fresh()
            engine.s.items.append("impossible_container")
            for _ in range(count):
                engine.add_ingredient("slag", emit=False)
            engine.spin()
            self.assertEqual(expected, engine.s.stats["last_income"])

    def test_crowded_lab_threshold_and_once_per_round(self) -> None:
        for count, expected in ((29, 0), (30, 3)):
            engine = self.fresh()
            engine.s.items.append("crowded_lab")
            for _ in range(count):
                engine.add_ingredient("slag", emit=False)
            engine.spin()
            self.assertEqual(expected, engine.s.gold)

        duplicate = self.fresh()
        duplicate.s.items[:] = ["crowded_lab", "crowded_lab"]
        for _ in range(30):
            duplicate.add_ingredient("slag", emit=False)
        duplicate.spin()
        self.assertEqual(3, duplicate.s.gold)

    def test_lucky_potion_guarantees_two_following_choices(self) -> None:
        engine = self.fresh()
        engine._apply_potion_payload(engine.catalog.ingredients["lucky_potion"]["potion"], "测试")
        first = engine.make_choice("ingredient")
        engine.s.pending.append(first)
        engine.choose(1)
        second = engine.make_choice("ingredient")
        engine.s.pending.append(second)
        engine.choose(1)
        self.assertTrue(any(engine.catalog.ingredients[x]["rarity"] >= 3 for x in first.offers))
        self.assertTrue(any(engine.catalog.ingredients[x]["rarity"] >= 3 for x in second.offers))
        self.assertEqual(0, engine.s.flags.get("choice_minimum_count", 0))

    def test_lucky_potion_roll_does_not_consume_extra_formal_choice(self) -> None:
        engine = self.fresh()
        engine._apply_potion_payload(engine.catalog.ingredients["lucky_potion"]["potion"], "测试")
        engine.s.pending.append(engine.make_choice("ingredient"))
        engine.s.tokens["roll"] = 1
        engine.reroll()
        self.assertEqual(2, engine.s.flags["choice_minimum_count"])
        engine.choose(1)
        self.assertEqual(1, engine.s.flags["choice_minimum_count"])

    def test_lucky_potion_remaining_count_survives_save_load(self) -> None:
        engine = self.fresh()
        engine._apply_potion_payload(engine.catalog.ingredients["lucky_potion"]["potion"], "测试")
        engine.s.pending.append(engine.make_choice("ingredient"))
        engine.choose(1)
        restored = GameEngine().bind(GameState.from_dict(engine.s.to_dict()))
        self.assertEqual(1, restored.s.flags["choice_minimum_count"])

    def test_paper_uses_forty_two_percent_growth_chance(self) -> None:
        engine = self.fresh()
        paper = engine.add_ingredient("paper", emit=False)
        engine._board = [paper]
        engine._coords = [(0, 0)]
        calls: list[float] = []
        engine._chance = lambda chance: calls.append(chance) or True
        engine._run_script(0, paper, "paper")
        self.assertEqual([0.42], calls)
        self.assertEqual(1, paper.permanent_bonus)
        self.assertTrue(paper.flags["grown"])

    def test_animal_registry_and_small_safe_use_updated_rewards(self) -> None:
        engine = self.fresh()
        engine.s.items.extend(["animal_registry", "small_safe"])
        engine.add_ingredient("kitten")
        self.assertEqual(7, engine.s.gold)
        engine._gain_token("roll", 1, "测试")
        self.assertEqual(11, engine.s.gold)

    def test_large_reactor_offers_two_choices_at_each_fixed_rarity(self) -> None:
        engine = self.fresh()
        engine.add_item("large_reactor")
        self.assertEqual(4, len(engine.s.pending))
        self.assertEqual([1, 1, 2, 2], [
            engine.catalog.ingredients[choice.offers[0]]["rarity"]
            for choice in engine.s.pending
        ])

    def test_magic_filter_prevents_every_negative_trigger(self) -> None:
        engine = self.fresh()
        engine.s.items.append("magic_filter")
        engine.r.random = lambda: 0.999999
        self.assertFalse(engine._chance(1.0, negative=True))
        self.assertEqual(1, engine.s.stats["event_counts"]["negative_prevented"])

    def test_monster_guide_checks_each_monster_independently_and_can_remove_multiple(self) -> None:
        engine = self.fresh()
        monsters = [engine.add_ingredient("goblin", emit=False) for _ in range(3)]
        engine.s.items.append("monster_guide")
        engine._board = list(monsters)
        engine._coords = [(0, 0), (0, 1), (0, 2)]
        engine._values = [0, 0, 0]
        draws = iter((0.10, 0.30, 0.10))
        calls: list[float] = []
        engine.r.random = lambda: calls.append(1.0) or next(draws, 0.99)
        engine._run_item_round_effects()
        self.assertEqual(1, len([x for x in engine.s.ingredients if "monster" in engine.catalog.ingredients[x.def_id].get("tags", [])]))
        self.assertEqual(2, engine.s.stats["event_counts"]["removed_tag:monster"])
        self.assertEqual(3, len(calls))

    def test_monster_already_removed_is_not_checked_again(self) -> None:
        engine = self.fresh()
        first = engine.add_ingredient("goblin", emit=False)
        second = engine.add_ingredient("goblin", emit=False)
        engine.s.items.append("monster_guide")
        engine._board = [first, second]
        engine._coords = [(0, 0), (0, 1)]
        engine._values = [0, 0]
        engine._remove(first, "removed", 0)
        calls: list[float] = []
        engine.r.random = lambda: calls.append(1.0) or 0.99
        engine._run_item_round_effects()
        self.assertEqual(1, len(calls))
        self.assertIn(second, engine.s.ingredients)

    def test_monster_guide_removal_events_trigger_essence_from_any_source(self) -> None:
        engine = self.fresh()
        monsters = [engine.add_ingredient("goblin", emit=False) for _ in range(3)]
        engine.add_essence("monster_guide_essence")
        for monster in monsters:
            self.assertTrue(engine._remove(monster, "removed", None))
        engine.check_essences()
        self.assertEqual(30, engine.s.gold)
        self.assertEqual(1, engine.s.tokens["remove"])
        self.assertIn("monster_guide_essence", engine.s.consumed_essences)

    def test_cyan_reagent_essence_requires_all_present_board_instances_unique(self) -> None:
        engine = self.fresh()
        first = engine.add_ingredient("snake", emit=False)
        second = engine.add_ingredient("snake", emit=False)
        engine.add_essence("cyan_reagent_essence")
        engine._board = [first, second]
        engine._coords = [(0, 0), (0, 1)]
        engine._values = [0, 0]

        engine.check_essences()
        self.assertEqual(0, engine.s.gold)
        self.assertIn("cyan_reagent_essence", engine.s.essences)

        # If one duplicate has already left the pool, the remaining visible
        # board is unique and the essence is allowed to trigger.
        engine.s.ingredients.remove(second)
        engine.check_essences()
        self.assertEqual(40, engine.s.gold)
        self.assertIn("cyan_reagent_essence", engine.s.consumed_essences)

    def test_auto_reroller_essence_starts_counting_when_acquired(self) -> None:
        engine = self.fresh()
        engine.s.pending = [PendingChoice(kind="ingredient", offers=["water"])]
        engine.s.tokens["roll"] = 2
        engine.reroll()
        engine.reroll()

        # Rerolls before acquisition must not satisfy the new essence.
        engine.add_essence("auto_reroller_essence")
        engine.check_essences()
        self.assertIn("auto_reroller_essence", engine.s.essences)

        engine.s.tokens["roll"] = 2
        engine.reroll()
        self.assertIn("auto_reroller_essence", engine.s.essences)

        # The second action may run in a fresh stateless agent process.
        engine = GameEngine().bind(GameState.from_dict(engine.s.to_dict()))
        engine.reroll()
        self.assertEqual(5, engine.s.tokens["roll"])
        self.assertIn("auto_reroller_essence", engine.s.consumed_essences)

    def test_new_counter_state_has_safe_default_for_old_save(self) -> None:
        engine = self.fresh()
        old_data = engine.s.to_dict()
        old_data["stats"].pop("spawn_counters", None)
        old_data["stats"].pop("round_events", None)
        old_data.pop("flags", None)
        resumed = GameEngine().bind(GameState.from_dict(old_data))
        self.assertEqual({}, resumed.s.stats["spawn_counters"])
        self.assertEqual({}, resumed.s.stats["round_events"])
        self.assertEqual(0, resumed.s.flags["choice_minimum_count"])

    def test_agent_payload_exposes_new_owned_definitions_and_counters(self) -> None:
        engine = self.fresh()
        for item_id in ("ore_sorting_table", "crowded_lab", "monster_guide"):
            engine.add_item(item_id)
        engine.add_essence("monster_guide_essence")
        payload = engine.agent_payload("status")
        self.assertEqual(
            {"ore_sorting_table", "crowded_lab", "monster_guide"},
            {row["id"] for row in payload["items_detail"]},
        )
        self.assertEqual("monster_guide_essence", payload["essences_detail"][0]["id"])
        self.assertIn("spawn_counters", payload["stats"])


if __name__ == "__main__":
    unittest.main()
