"""Public single-action reloads must retain the current round's context."""
from copy import deepcopy
import json
import unittest

from crucible_echoes.engine import GameEngine
from crucible_echoes.model import GameState


class RoundRuntimePersistenceTests(unittest.TestCase):
    def restore(self, engine):
        state = GameState.from_dict(json.loads(json.dumps(engine.s.to_dict())))
        return GameEngine(engine.catalog).bind(state)

    def fixture(self):
        engine = GameEngine()
        engine.new_game(20261020)
        engine.s.ingredients.clear()
        high = max((row for row in engine.catalog.ingredients.values()
                    if row.get("removable", True) and not row.get("on_removed") and not row.get("on_acquire")),
                   key=lambda row: int(row.get("base", 0)))
        engine.add_ingredient(high["id"], emit=False)
        for _ in range(4):
            engine.add_ingredient("water", emit=False)
        engine.s.tokens["remove"] = 4
        engine.add_essence("equivalent_exchange_essence")
        engine._sync_rng()
        return engine, int(high["base"])

    def test_four_manual_removals_use_same_round_highest_value_across_reloads(self):
        live, high_value = self.fixture()
        resumed = self.restore(live)
        self.assertGreater(high_value, int(live.catalog.ingredients["water"]["base"]))
        for index in range(4):
            live.remove(1)
            resumed.remove(1)
            self.assertEqual(live.s.to_dict(), resumed.s.to_dict(), f"manual removal {index + 1}")
            self.assertEqual(live.r.state, resumed.r.state)
            resumed = self.restore(resumed)
        self.assertEqual(1, len(live.s.ingredients))
        self.assertEqual(high_value, live.s.ingredients[0].permanent_bonus)
        self.assertIn("equivalent_exchange_essence", resumed.s.consumed_essences)

    def test_round_event_values_survive_json_but_reset_on_next_spin(self):
        engine = GameEngine()
        engine.new_game(20261020)
        engine.s.ingredients.clear()
        engine.add_ingredient("water", emit=False)
        engine.emit("stored", value=7)
        engine._sync_rng()
        resumed = self.restore(engine)
        self.assertEqual(dict(engine._round_event_values), dict(resumed._round_event_values))
        resumed.spin()
        self.assertEqual({}, dict(resumed._round_event_values))
        self.assertEqual({}, dict(self.restore(resumed)._round_event_values))

    def test_removed_values_reset_each_round_and_do_not_leak_on_rebind(self):
        engine, high_value = self.fixture()
        engine.remove(1)
        restored = self.restore(engine)
        self.assertEqual([(int(engine.catalog.ingredients[engine.s.removed_history[-1]]["rarity"]), high_value)], restored._removed_values)
        restored.spin()
        self.assertEqual([], restored._removed_values)
        self.assertEqual([], self.restore(restored)._removed_values)
        engine.bind(deepcopy(restored.s))
        self.assertEqual([], engine._removed_values)

    def test_legacy_missing_context_has_safe_empty_defaults_without_guessing_history(self):
        engine, _ = self.fixture()
        raw = engine.s.to_dict()
        raw["stats"].pop("round_removed_values", None)
        raw["stats"].pop("round_event_values", None)
        raw["removed_history"] = ["water"]  # Global history is not a round ledger.
        target = self.restore(engine)
        target._removed_values = [(4, 999)]
        target._round_event_values["old"] = 999
        target.bind(GameState.from_dict(raw))
        self.assertEqual([], target._removed_values)
        self.assertEqual({}, dict(target._round_event_values))
        self.assertEqual(engine.r.state, target.r.state)


if __name__ == "__main__":
    unittest.main()
