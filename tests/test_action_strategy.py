from copy import deepcopy
import unittest

from crucible_echoes.action_strategy import ContentActionStrategy
from crucible_echoes.content_strategy import ContentAwareV2RevisionStrategy
from crucible_echoes.engine import GameEngine
from crucible_echoes.model import PendingChoice
from crucible_echoes.simulation import run_batch, simulate_game, strategy_from_name


class ActionStrategyTests(unittest.TestCase):
    def engine(self, size=5):
        engine = GameEngine()
        engine.new_game(20261003, 7)
        engine.s.ingredients.clear()
        engine.s.pending.clear()
        for _ in range(size):
            engine.add_ingredient("water", emit=False)
        return engine

    def bundle(self, ids, reverse=False):
        offers = ["take_data", "leave_data"]
        if reverse:
            offers.reverse()
        return PendingChoice("bundle", offers, can_skip=False, details={"options": {
            "take_data": {"add_ingredients": ids}, "leave_data": {}}})

    def test_decisions_do_not_modify_catalog_state_or_rng(self):
        engine = self.engine(25)
        engine.s.items.extend(["large_material_pack", "ban"])
        engine.add_ingredient("magic_magic", emit=False)
        policy = ContentActionStrategy()
        before = deepcopy(engine.s.to_dict()), deepcopy(engine.catalog), engine.r.state
        for _ in range(3):
            policy.pre_spin_action(engine)
            policy.bundle_index(engine, self.bundle(["red_pigment", "yellow_pigment", "blue_pigment"]))
            policy.removal_index(engine)
        self.assertEqual(before, (engine.s.to_dict(), engine.catalog, engine.r.state))

    def test_bundle_uses_contents_not_id_or_first_option(self):
        policy = ContentActionStrategy()
        ids = ["red_pigment", "yellow_pigment", "blue_pigment"]
        for reverse in (False, True):
            choice = self.bundle(ids, reverse)
            chosen = policy.bundle_index(self.engine(5), choice)
            self.assertEqual("take_data", choice.offers[chosen - 1])
            chosen = policy.bundle_index(self.engine(28), choice)
            self.assertEqual("leave_data", choice.offers[chosen - 1])

    def test_consumable_choice_pack_is_generic_and_not_repeated(self):
        engine = self.engine()
        row = deepcopy(engine.catalog.items["large_material_pack"])
        row["id"] = "renamed_active_pack"
        engine.catalog.items[row["id"]] = row
        engine.s.items.append(row["id"])
        policy = ContentActionStrategy()
        self.assertEqual({"action": "use", "item_id": row["id"]}, policy.pre_spin_action(engine))
        engine.use_item(row["id"])
        self.assertEqual(4, len(engine.s.pending))
        self.assertNotIn(row["id"], engine.s.items)
        while engine.s.pending:
            engine.skip()
        self.assertIsNone(policy.pre_spin_action(engine))

    def test_large_pool_does_not_force_using_ordinary_pack(self):
        engine = self.engine(28)
        engine.s.items.append("large_material_pack")
        self.assertIsNone(ContentActionStrategy().pre_spin_action(engine))

    def test_order_book_is_prepared_only_when_affordable_and_only_once(self):
        engine = self.engine()
        engine.s.spins_left = 1
        engine.s.items.append("lucky_order_book")
        policy = ContentActionStrategy()
        self.assertIsNone(policy.pre_spin_action(engine))
        engine.s.gold = engine.current_order()[0]
        self.assertEqual("use", policy.pre_spin_action(engine)["action"])
        engine.use_item("lucky_order_book")
        self.assertIsNone(policy.pre_spin_action(engine))

    def test_permanent_generation_ban_cannot_be_toggled_back_on(self):
        engine = self.engine(25)
        engine.s.items.append("ban")
        engine.add_ingredient("magic_magic", emit=False)
        engine.s.flags["ingredient_generation_permanently_disabled"] = True
        policy = ContentActionStrategy()
        self.assertIsNone(policy.pre_spin_action(engine))
        self.assertEqual(0, policy._generator_penalty(engine, engine.catalog.ingredients["magic_magic"]))

    def test_generation_switch_has_a_stable_desired_state(self):
        engine = self.engine(25)
        engine.s.items.append("ban")
        for _ in range(3):
            engine.add_ingredient("summon_magic", emit=False)
        policy = ContentActionStrategy()
        action = policy.pre_spin_action(engine)
        self.assertEqual({"action": "toggle", "item_id": "ban"}, action)
        engine.toggle_item("ban")
        self.assertIsNone(policy.pre_spin_action(engine))
        engine.s.ingredients = engine.s.ingredients[:8]
        self.assertEqual({"action": "toggle", "item_id": "ban"}, policy.pre_spin_action(engine))

    def test_tactical_removal_can_cash_out_to_save_small_pool_order(self):
        engine = self.engine(11)
        monster = engine.add_ingredient("goblin", emit=False)
        monster.stored_gold = 24
        engine.s.gold = 0
        engine.s.spins_left = 1
        engine.s.tokens["remove"] = 1
        self.assertIsNone(ContentAwareV2RevisionStrategy().removal_index(engine))
        index = ContentActionStrategy().removal_index(engine)
        self.assertEqual(12, index)
        engine.remove(index)
        engine.spin()
        self.assertEqual("playing", engine.s.status)
        self.assertEqual(1, engine.s.order_index)
        self.assertEqual(10, engine.s.gold)

    def test_affordable_order_does_not_force_cashing_out_core(self):
        engine = self.engine(11)
        monster = engine.add_ingredient("goblin", emit=False)
        monster.stored_gold = 24
        engine.s.spins_left = 1
        engine.s.gold = 100
        engine.s.tokens["remove"] = 1
        self.assertIsNone(ContentActionStrategy().removal_index(engine))

    def test_income_estimate_includes_declared_flat_item_income(self):
        engine = self.engine(11)
        stored = engine.add_ingredient("goblin", emit=False)
        stored.stored_gold = 24
        engine.s.items.extend(["worker", "coin_pouch", "treasury"])
        engine.s.spins_left = 1
        engine.s.tokens["remove"] = 1
        policy = ContentActionStrategy()
        # Existing per_spin_gold: 1 + 1 + 4, not an invented per_spin field.
        stable = sum(engine.stable_ingredient_value(x) for x in engine.s.ingredients)
        self.assertEqual(stable + 6, policy._income_estimate(engine))
        engine.s.gold = engine.current_order()[0] - stable - 6
        self.assertIsNone(policy.removal_index(engine))

    def test_simulator_uses_active_pack_and_records_item_growth(self):
        def start(engine):
            engine.s.items.append("large_material_pack")
        record = simulate_game(20261003, 7, strategy=ContentActionStrategy(), on_start=start)
        self.assertNotEqual("aborted", record.status, record.error)
        self.assertGreaterEqual(record.content_stats["items"]["large_material_pack"]["consumed_count"], 1)
        self.assertTrue(any(x["action"] == "use" for x in record.strategy_events["active_actions"]))
        self.assertGreater(record.strategy_events["pool_source_counts"]["item_generation"], 0)

    def test_invalid_active_action_aborts_instead_of_becoming_game_loss(self):
        class InvalidPolicy(ContentAwareV2RevisionStrategy):
            def pre_spin_action(self, engine):
                return {"action": "use", "item_id": "unowned_item"}
        record = simulate_game(20261003, 7, strategy=InvalidPolicy())
        self.assertEqual("aborted", record.status)
        self.assertIn("不合法", record.error)

    def test_nonprogressing_use_aborts_before_action_budget(self):
        class RepeatingPolicy(ContentAwareV2RevisionStrategy):
            def pre_spin_action(self, engine):
                return {"action": "use", "item_id": "lucky_order_book"}
        def start(engine):
            engine.s.items.append("lucky_order_book")
            engine.s.spins_left = 1
        record = simulate_game(42, 7, strategy=RepeatingPolicy(), on_start=start)
        self.assertEqual("aborted", record.status)
        self.assertIn("未推进", record.error)
        self.assertEqual(1, record.action_count)

    def test_toggle_loop_is_bounded_without_changing_game_failure_rules(self):
        class TogglingPolicy(ContentAwareV2RevisionStrategy):
            def pre_spin_action(self, engine):
                return {"action": "toggle", "item_id": "ban"}
        record = simulate_game(42, 7, strategy=TogglingPolicy(), on_start=lambda e: e.s.items.append("ban"))
        self.assertEqual("aborted", record.status)
        self.assertIn("过多", record.error)
        self.assertEqual(32, record.action_count)

    def test_legacy_default_hooks_preserve_full_records(self):
        class ExplicitLegacy(ContentAwareV2RevisionStrategy):
            def pre_spin_action(self, engine):
                return None
            def bundle_index(self, engine, choice):
                return 1
        original = run_batch(2, 42, 7, strategy=ContentAwareV2RevisionStrategy())
        explicit = run_batch(2, 42, 7, strategy=ExplicitLegacy())
        self.assertEqual(original.to_dict(), explicit.to_dict())

    def test_registered_new_policy_and_batch_are_reproducible(self):
        self.assertIsInstance(strategy_from_name("heuristic-v2-content-v3"), ContentActionStrategy)
        one = run_batch(2, 20261003, 7, strategy=ContentActionStrategy())
        two = run_batch(2, 20261003, 7, strategy=ContentActionStrategy())
        self.assertEqual(one.to_dict(), two.to_dict())
        self.assertEqual(0, one.summary["aborted"])


if __name__ == "__main__":
    unittest.main()
