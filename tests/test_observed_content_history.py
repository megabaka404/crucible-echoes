from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from crucible_echoes.engine import GameEngine
from crucible_echoes.model import GameState, PendingChoice
from desktop.bridge import DesktopBridge
from desktop.view_model import build_view_state


class ObservedContentHistoryTests(unittest.TestCase):
    def fresh(self):
        engine = GameEngine()
        engine.new_game(20261025)
        return engine

    def ids(self, engine, kind):
        return {row["id"] for row in build_view_state(engine)["codex"][kind]}

    def test_consumed_active_item_stays_in_codex_after_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "save.json"
            bridge = DesktopBridge(path)
            bridge.new_game(20261025)
            bridge._engine.s.pending.append(PendingChoice(kind="item", offers=["sandpaper_box"]))
            bridge.action("choose", {"number": 1})
            result = bridge.action("use", {"item_id": "sandpaper_box"})
            self.assertNotIn("sandpaper_box", bridge._engine.s.items)
            self.assertIn("sandpaper_box", {x["id"] for x in result["codex"]["items"]})
            reloaded = DesktopBridge(path).get_state()
            self.assertIn("sandpaper_box", {x["id"] for x in reloaded["codex"]["items"]})

    def test_resolved_unselected_candidate_stays_known_without_gain_event(self):
        engine = self.fresh()
        engine.s.pending.append(PendingChoice(kind="ingredient", offers=["water", "paper"]))
        engine._sync_rng()  # A published reward snapshot, not a draw trial.
        events = deepcopy(engine.s.stats["event_counts"])
        engine.skip()
        self.assertIn("paper", self.ids(engine, "ingredients"))
        self.assertNotIn("paper", {x.def_id for x in engine.s.ingredients})
        self.assertEqual(events.get("ingredient_added", 0), engine.s.stats["event_counts"].get("ingredient_added", 0))

    def test_discarded_draw_trials_are_not_discoveries(self):
        engine = self.fresh()
        old_history = deepcopy(engine.s.stats.get("observed_content", {}))
        engine.make_choice("item", fixed_rarity=4)  # Not in any published queue.
        engine._sync_rng()
        self.assertEqual(old_history, engine.s.stats.get("observed_content", {}))

    def test_history_sync_is_rng_free_deduplicated_and_view_is_read_only(self):
        engine = self.fresh()
        engine.s.pending.append(PendingChoice(kind="item", offers=["worker", "worker"]))
        engine._sync_rng()
        self.assertEqual(["worker"], engine.s.stats["observed_content"]["items"])
        before, rng = deepcopy(engine.s.to_dict()), engine.r.state
        engine._sync_rng()
        self.assertEqual(before, engine.s.to_dict())
        self.assertEqual(rng, engine.r.state)
        self.ids(engine, "items")
        self.assertEqual(before, engine.s.to_dict())

    def test_legacy_missing_history_loads_and_future_discoveries_are_saved(self):
        engine = self.fresh()
        raw = engine.s.to_dict()
        raw["stats"].pop("observed_content", None)
        legacy = GameEngine().bind(GameState.from_dict(raw))
        rng = legacy.r.state
        legacy.s.pending.append(PendingChoice(kind="essence", offers=["spare_beaker_essence"]))
        legacy._sync_rng()
        legacy.skip()
        loaded = GameEngine().bind(GameState.from_dict(legacy.s.to_dict()))
        self.assertIn("spare_beaker_essence", self.ids(loaded, "essences"))
        self.assertEqual(rng, loaded.r.state)
        self.assertNotIn("golden_lucky_core", self.ids(loaded, "items"))


if __name__ == "__main__":
    unittest.main()
