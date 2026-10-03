from __future__ import annotations

from collections import Counter, defaultdict
import unittest

from crucible_echoes.engine import GameEngine
from crucible_echoes.geometry import board_coords
from crucible_echoes.model import GameState, PendingChoice


class NewItemTests(unittest.TestCase):
    def fresh(self) -> GameEngine:
        engine = GameEngine()
        engine.new_game(20260820)
        engine.s.ingredients.clear()
        engine.s.items.clear()
        engine.s.essences.clear()
        engine.s.consumed_essences.clear()
        engine.s.pending.clear()
        engine.s.gold = 0
        engine._round_events = defaultdict(int)
        engine._round_event_values = defaultdict(int)
        return engine

    def complete_current_order(self, engine: GameEngine) -> int:
        amount, _ = engine.current_order()
        engine.s.gold = amount
        engine._settle_order()
        engine.s.pending.clear()
        return amount

    def test_new_item_ids_are_unique_and_rarities_are_correct(self) -> None:
        catalog = GameEngine().catalog
        new_rarities = {
            "spare_beaker": 2,
            "old_ledger": 1,
            "order_appendix": 1,
            "wastepaper_box": 1,
            "piggy_bank": 1,
            "animal_roster": 1,
            "prism_mount": 1,
            "paperweight": 1,
            "fuel_reservoir": 1,
            "three_slot_margin": 1,
            "sandpaper_box": 1,
            "human_roster": 1,
            "easter_egg_box": 1,
            "old_catalog": 2,
            "supply_subscription": 3,
            "potion_catalyst": 3,
        }
        self.assertEqual(new_rarities, {item_id: catalog.items[item_id]["rarity"] for item_id in new_rarities})
        self.assertEqual(len(catalog.items), len(set(catalog.items)))
        self.assertEqual(len(catalog.essences), len(set(catalog.essences)))
        for essence_id in (
            "spare_beaker_essence", "old_ledger_essence", "wastepaper_box_essence",
            "piggy_bank_essence", "paperweight_essence", "fuel_reservoir_essence",
            "three_slot_margin_essence", "easter_egg_box_essence", "old_catalog_essence",
        ):
            self.assertIn(essence_id, catalog.essences)

    def test_spare_beaker_two_choices_and_essence_bonus(self) -> None:
        engine = self.fresh()
        engine.add_ingredient("water", emit=False)
        engine.s.items.append("spare_beaker")
        engine.s.pending.append(engine.make_choice("ingredient"))
        engine.choose(1)
        restored = GameEngine().bind(GameState.from_dict(engine.s.to_dict()))
        restored.s.pending.append(restored.make_choice("ingredient"))
        restored.choose(1)
        self.assertEqual(4, len(restored.make_choice("ingredient").offers))
        self.assertEqual(3, len(restored.make_choice("ingredient").offers))

        essence = self.fresh()
        essence.add_essence("spare_beaker_essence")
        choice = essence.make_choice("ingredient")
        self.assertEqual(5, len(choice.offers))
        self.assertIn("spare_beaker_essence", essence.s.consumed_essences)

    def test_order_items_and_essences_only_trigger_after_success(self) -> None:
        engine = self.fresh()
        engine.s.items.append("old_ledger")
        engine.add_essence("old_ledger_essence")
        amount = self.complete_current_order(engine)
        self.assertEqual(5 + int(amount * 0.20), engine.s.gold)
        self.assertIn("old_ledger_essence", engine.s.consumed_essences)

        failed = self.fresh()
        failed.s.items.append("old_ledger")
        failed.add_essence("old_ledger_essence")
        failed._settle_order()
        self.assertEqual("lost", failed.s.status)
        self.assertEqual(0, failed.s.gold)
        self.assertIn("old_ledger_essence", failed.s.essences)

    def test_wastepaper_box_counts_orders_and_essence_grants_two_tokens(self) -> None:
        engine = self.fresh()
        engine.s.items.append("wastepaper_box")
        for _ in range(3):
            self.complete_current_order(engine)
        self.assertEqual(1, engine.s.tokens["remove"])

        essence = self.fresh()
        essence.add_essence("wastepaper_box_essence")
        self.complete_current_order(essence)
        self.assertEqual(2, essence.s.tokens["remove"])
        self.assertIn("wastepaper_box_essence", essence.s.consumed_essences)

    def test_piggy_bank_storage_withdrawal_essence_and_save_round_trip(self) -> None:
        engine = self.fresh()
        engine.s.items.append("piggy_bank")
        self.complete_current_order(engine)
        self.assertEqual(5, engine.s.stats["item_storage"]["piggy_bank"])

        restored = GameEngine().bind(GameState.from_dict(engine.s.to_dict()))
        self.assertEqual(5, restored.s.stats["item_storage"]["piggy_bank"])
        amount, _ = restored.current_order()
        restored.s.gold = amount - 5
        restored._settle_order()
        self.assertEqual(2, restored.s.order_index)
        self.assertEqual(5, restored.s.stats["item_storage"]["piggy_bank"])

        essence = self.fresh()
        essence.add_essence("piggy_bank_essence")
        self.complete_current_order(essence)
        self.assertEqual(30, essence.s.stats["item_storage"]["piggy_bank"])
        self.assertIn("piggy_bank_essence", essence.s.consumed_essences)

    def test_animal_and_human_rosters_filter_choices_and_rerolls(self) -> None:
        for item_id, tag, expected_groups in (("animal_roster", "animal", 3), ("human_roster", "human", 2)):
            engine = self.fresh()
            engine.add_item(item_id)
            self.assertEqual(expected_groups, len(engine.s.pending))
            for choice in engine.s.pending:
                self.assertEqual(tag, choice.tag_filter)
                self.assertTrue(all(tag in engine.catalog.ingredients[def_id].get("tags", []) for def_id in choice.offers))
            engine.s.tokens["roll"] = 1
            engine.reroll()
            self.assertEqual(tag, engine.s.pending[0].tag_filter)
            self.assertTrue(all(tag in engine.catalog.ingredients[def_id].get("tags", []) for def_id in engine.s.pending[0].offers))

    def test_static_tag_bonus_items_apply_declared_values(self) -> None:
        engine = self.fresh()
        engine.s.items.extend(["prism_mount", "paperweight", "fuel_reservoir"])
        self.assertEqual(1, engine._item_bonus(engine.catalog.ingredients["copper_prism"]))
        self.assertEqual(2, engine._item_bonus(engine.catalog.ingredients["paper"]))
        self.assertEqual(2, engine._item_bonus(engine.catalog.ingredients["charcoal"]))

    def test_paperweight_and_fuel_essences_apply_contextual_permanent_bonus(self) -> None:
        paper_engine = self.fresh()
        paper = paper_engine.add_ingredient("paper", emit=False)
        paper_engine.add_essence("paperweight_essence")
        paper_engine._permanent_bonus(paper, 1)
        self.assertEqual(3, paper.permanent_bonus)
        self.assertIn("paperweight_essence", paper_engine.s.consumed_essences)

        fuel_engine = self.fresh()
        charcoal = fuel_engine.add_ingredient("charcoal", emit=False)
        fuel_engine.add_essence("fuel_reservoir_essence")
        fuel_engine.spin()
        self.assertEqual(3, charcoal.permanent_bonus)
        self.assertIn("fuel_reservoir_essence", fuel_engine.s.consumed_essences)

    def test_three_slot_margin_and_essence_use_actual_empty_slots(self) -> None:
        engine = self.fresh()
        for _ in range(17):
            engine.add_ingredient("water", emit=False)
        engine.s.items.append("three_slot_margin")
        engine.add_essence("three_slot_margin_essence")
        engine.spin()
        self.assertEqual(40, engine.s.gold)
        self.assertIn("three_slot_margin_essence", engine.s.consumed_essences)

        crowded = self.fresh()
        for _ in range(18):
            crowded.add_ingredient("water", emit=False)
        crowded.s.items.append("three_slot_margin")
        crowded.add_essence("three_slot_margin_essence")
        crowded.spin()
        self.assertEqual(18, crowded.s.gold)
        self.assertIn("three_slot_margin_essence", crowded.s.essences)

    def test_optional_boxes_have_real_active_use_actions(self) -> None:
        for item_id, ingredient_id in (("sandpaper_box", "sandpaper"), ("easter_egg_box", "easter_egg")):
            engine = self.fresh()
            engine.add_item(item_id)
            self.assertIn(f"use {item_id}", engine.agent_available_actions())
            engine.use_item(item_id)
            self.assertNotIn(item_id, engine.s.items)
            self.assertEqual(2, Counter(x.def_id for x in engine.s.ingredients)[ingredient_id])

    def test_easter_egg_box_essence_triggers_on_acquire(self) -> None:
        engine = self.fresh()
        engine.add_essence("easter_egg_box_essence")
        engine.check_essences()
        self.assertEqual(3, Counter(x.def_id for x in engine.s.ingredients)["easter_egg"])
        self.assertIn("easter_egg_box_essence", engine.s.consumed_essences)

    def test_old_catalog_stacks_skips_and_essence_creates_three_choices(self) -> None:
        engine = self.fresh()
        engine.s.items.append("old_catalog")
        engine.s.pending.extend([engine.make_choice("ingredient"), engine.make_choice("ingredient")])
        engine.skip()
        engine.skip()
        choice = engine.make_choice("ingredient")
        self.assertEqual(5, len(choice.offers))
        self.assertNotIn("ingredient_choice_extra", engine.s.flags)

        essence = self.fresh()
        essence.add_essence("old_catalog_essence")
        essence.s.pending.append(essence.make_choice("ingredient"))
        essence.skip()
        self.assertEqual(3, len(essence.s.pending))
        self.assertIn("old_catalog_essence", essence.s.consumed_essences)

    def test_supply_subscription_draws_one_valid_common_item(self) -> None:
        engine = self.fresh()
        engine.s.items.append("supply_subscription")
        before = set(engine.s.items)
        self.complete_current_order(engine)
        gained = set(engine.s.items) - before
        self.assertEqual(1, len(gained))
        gained_id = next(iter(gained))
        self.assertEqual(1, engine.catalog.items[gained_id]["rarity"])

    def test_potion_catalyst_doubles_payload_quantities_and_values(self) -> None:
        engine = self.fresh()
        engine.s.items.append("potion_catalyst")
        engine._apply_potion_payload({"gold": 10}, "测试")
        self.assertEqual(20, engine.s.gold)
        engine._apply_potion_payload({"token": "roll", "amount": 1}, "测试")
        self.assertEqual(2, engine.s.tokens["roll"])
        engine._apply_potion_payload({"choice_minimum": {"minimum": 3, "count": 2}}, "测试")
        self.assertEqual(3, engine.s.flags["choice_minimum_rarity"])
        self.assertEqual(4, engine.s.flags["choice_minimum_count"])

        engine.s.removed_history[:] = ["water"]
        engine._apply_potion_payload({"recycle": True}, "测试")
        self.assertEqual(2, Counter(x.def_id for x in engine.s.ingredients)["water"])
        engine._apply_potion_payload({"purify": True}, "测试")
        self.assertEqual(0, len(engine.s.ingredients))

    def test_potion_catalyst_doubles_item_copy_and_temporary_value_effects(self) -> None:
        item_engine = self.fresh()
        item_engine.s.items.append("potion_catalyst")
        item_engine._apply_potion_payload({"item_rarity": 1}, "测试")
        self.assertEqual(3, len(item_engine.s.items))
        self.assertTrue(all(item_engine.catalog.items[item_id]["rarity"] in {1, 3} for item_id in item_engine.s.items))

        copy_engine = self.fresh()
        potion = copy_engine.add_ingredient("copy_potion", emit=False)
        target = copy_engine.add_ingredient("water", emit=False)
        copy_engine.s.items.append("potion_catalyst")
        copy_engine._board = [potion, target]
        copy_engine._coords = [(0, 0), (0, 1)]
        copy_engine._values = [0, 1]
        copy_engine._run_script(0, potion, "copy_potion")
        self.assertEqual(3, Counter(x.def_id for x in copy_engine.s.ingredients)["water"])
        self.assertEqual(2, copy_engine.s.stats["event_counts"]["copied"])

        double_engine = self.fresh()
        double_potion = double_engine.add_ingredient("double_potion", emit=False)
        water = double_engine.add_ingredient("water", emit=False)
        double_engine.s.items.append("potion_catalyst")
        double_engine._board = [double_potion, water]
        double_engine._coords = [(0, 0), (0, 1)]
        self.assertEqual(4, double_engine._apply_multipliers([0, 1])[1])

    def test_each_destroyed_potion_emits_one_potion_event(self) -> None:
        engine = self.fresh()
        potion = engine.add_ingredient("wealth_potion", emit=False)
        engine._board = [potion]
        engine._coords = [(0, 0)]
        engine._values = [0]
        engine._trigger_potion(0, potion, engine.catalog.ingredients["wealth_potion"]["potion"])
        self.assertEqual(1, engine.s.stats["event_counts"]["potion"])

    def test_old_save_defaults_cover_new_storage_and_choice_filter(self) -> None:
        engine = self.fresh()
        data = engine.s.to_dict()
        data["stats"].pop("item_storage", None)
        restored_state = GameState.from_dict(data)
        self.assertEqual({}, restored_state.stats["item_storage"])
        restored = GameEngine().bind(restored_state)
        self.assertEqual({}, restored.s.stats["item_storage"])
        choice = PendingChoice.from_dict({"kind": "ingredient", "offers": ["water"]})
        self.assertIsNone(choice.tag_filter)


if __name__ == "__main__":
    unittest.main()
