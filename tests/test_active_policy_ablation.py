from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from crucible_echoes.action_strategy import ContentActionStrategy
from crucible_echoes.engine import GameEngine
from tools.active_policy_ablation import SuppressedActivePolicy, run_experiment


class ActivePolicyAblationTests(unittest.TestCase):
    def test_only_decision_capability_is_hidden_without_state_or_rng_changes(self):
        engine = GameEngine()
        engine.new_game(42, 7)
        engine.s.items.append("lucky_order_book")
        engine.s.spins_left = 1
        engine.s.gold = 1000
        before = deepcopy(engine.s.to_dict()), deepcopy(engine.catalog), engine.r.state
        self.assertEqual("use", ContentActionStrategy().pre_spin_action(engine)["action"])
        self.assertIsNone(SuppressedActivePolicy("order_book").pre_spin_action(engine))
        self.assertEqual(before, (engine.s.to_dict(), engine.catalog, engine.r.state))
        # The actual game still exposes and can execute the item operation.
        self.assertTrue(any(x.get("item_id") == "lucky_order_book" for x in engine.agent_action_specs()))
        engine.use_item("lucky_order_book")
        self.assertTrue(engine.s.flags["order_book_sacrifice"])

    def test_other_active_operations_are_not_suppressed(self):
        engine = GameEngine()
        engine.new_game(42, 7)
        engine.s.items.extend(["lucky_order_book", "sandpaper_box"])
        engine.s.spins_left = 1
        engine.s.gold = 1000
        policy = SuppressedActivePolicy("order_book")
        self.assertEqual({"action": "use", "item_id": "sandpaper_box"}, policy.pre_spin_action(engine))

    def test_experiment_checks_effect_field_and_preserves_existing_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ablation.json"
            with self.assertRaisesRegex(ValueError, "Unknown active"):
                run_experiment(field="lucky_order_book", games=1, seed=42, difficulties=[7], output=path)
            report = run_experiment(field="order_book", games=1, seed=42, difficulties=[7], output=path)
            self.assertTrue(report["complete"])
            self.assertEqual(1, report["difficulties"]["7"]["games"])
            self.assertIn("class SuppressedActivePolicy", report["analysis_source"])
            with self.assertRaisesRegex(ValueError, "Existing analysis"):
                run_experiment(field="order_book", games=1, seed=42, difficulties=[7], output=path)


if __name__ == "__main__":
    unittest.main()
