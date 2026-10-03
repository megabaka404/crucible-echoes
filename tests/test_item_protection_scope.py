from copy import deepcopy
import unittest

from crucible_echoes.engine import GameEngine
from crucible_echoes.model import GameState


class ItemProtectionScopeTests(unittest.TestCase):
    def fresh(self, *ids):
        engine = GameEngine()
        engine.new_game(42)
        engine.s.ingredients.clear()
        engine.s.items.append("advanced_tube_rack")
        for name in ids:
            engine.add_ingredient(name, emit=False)
        return engine

    def test_non_glass_removal_is_not_prevented_or_counted(self):
        for name in ("cauldron", "water", "spring"):
            with self.subTest(name=name):
                engine = self.fresh(name)
                target = engine.s.ingredients[0]
                rng = engine.r.state
                self.assertTrue(engine._remove(target, "shattered", None))
                self.assertEqual(rng, engine.r.state)
                self.assertNotIn("advanced_rack_saved", target.flags)
                self.assertEqual(0, engine.s.stats.get("event_counts", {}).get("shatter_prevented", 0))

    def test_glass_protection_is_per_instance_and_only_once(self):
        engine = self.fresh("test_tube", "test_tube")
        first, second = engine.s.ingredients
        rng = engine.r.state
        for target in (first, second):
            self.assertFalse(engine._remove(target, "shattered", None))
            self.assertTrue(target.flags["advanced_rack_saved"])
        self.assertEqual(2, engine.s.stats["event_counts"]["shatter_prevented"])
        self.assertTrue(engine._remove(first, "shattered", None))
        self.assertTrue(engine._present(second))
        self.assertEqual(rng, engine.r.state)

    def test_existing_glass_tag_scope_and_other_removal_reasons_stay_unchanged(self):
        engine = self.fresh("glass_bead", "test_tube", "glass_shard")
        bead, tube, shard = engine.s.ingredients
        self.assertFalse(engine._remove(bead, "shattered", None))
        self.assertTrue(engine._remove(tube, "manual", None))
        self.assertTrue(engine._remove(shard, "expired", None))
        self.assertEqual(1, engine.s.stats["event_counts"]["shatter_prevented"])

    def test_old_saved_protection_flag_survives_reload(self):
        engine = self.fresh("test_tube")
        engine.s.ingredients[0].flags["advanced_rack_saved"] = True
        engine = GameEngine().bind(GameState.from_dict(deepcopy(engine.s.to_dict())))
        self.assertTrue(engine._remove(engine.s.ingredients[0], "shattered", None))
        self.assertEqual(0, engine.s.stats.get("event_counts", {}).get("shatter_prevented", 0))

    def test_three_real_preventions_trigger_essence_but_wrong_tags_do_not(self):
        engine = self.fresh("cauldron", "test_tube", "flask", "measuring_cylinder")
        engine.add_essence("advanced_tube_rack_essence")
        for index, target in enumerate(list(engine.s.ingredients)):
            engine._remove(target, "shattered", None)
            engine.check_essences()
            self.assertEqual(index == 3, "advanced_tube_rack_essence" in engine.s.consumed_essences)
        self.assertEqual(3, engine.s.stats["event_counts"]["shatter_prevented"])

    def test_generic_rule_works_with_renamed_item_and_all_tag_filter(self):
        engine = self.fresh("test_tube", "glass_bead")
        rule = deepcopy(engine.catalog.items["advanced_tube_rack"])
        rule["id"] = "scope_probe"
        rule["protect_once"] = {"reason": "shattered", "tags_all": ["glass", "equipment"]}
        engine.catalog.items["scope_probe"] = rule
        engine.s.items = ["scope_probe"]
        tube, bead = engine.s.ingredients
        self.assertFalse(engine._remove(tube, "shattered", None))
        self.assertTrue(tube.flags["protection_used:scope_probe"])
        self.assertTrue(engine._remove(tube, "shattered", None))
        self.assertTrue(engine._remove(bead, "shattered", None))
