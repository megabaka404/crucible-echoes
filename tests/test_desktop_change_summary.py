from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from crucible_echoes.engine import GameEngine
from crucible_echoes.save import load_game
from desktop.bridge import DesktopBridge
from desktop.change_summary import action_changes


class DesktopChangeSummaryTests(unittest.TestCase):
    def engine(self):
        engine = GameEngine()
        engine.new_game(20261019)
        engine.s.ingredients.clear()
        engine.add_ingredient("water", emit=False)
        engine.add_ingredient("paper", emit=False)
        return engine

    def clone(self, engine):
        return deepcopy(engine, {id(engine.catalog): engine.catalog})

    def test_uid_changes_distinguish_add_remove_transform_and_permanent(self):
        before = self.engine()
        after = self.clone(before)
        old_water, paper = after.s.ingredients
        after._remove(old_water, "manual", None)
        after._transform(paper, "water")
        after._permanent_bonus(paper, 3)
        added = after.add_ingredient("paper", emit=False)
        changes = action_changes(before, after)
        self.assertEqual("action_boundary", changes["scope"])
        self.assertEqual(["removed", "transformed", "permanent", "added"], [x["kind"] for x in changes["ingredients"]])
        self.assertEqual(paper.uid, changes["ingredients"][1]["uid"])
        self.assertEqual("paper", changes["ingredients"][1]["previous_id"])
        self.assertEqual(3, changes["ingredients"][2]["delta"])
        self.assertEqual(added.uid, changes["ingredients"][3]["uid"])
        self.assertNotIn("reason", changes["ingredients"][0])
        self.assertNotIn("effect_source", changes)

    def test_gold_tokens_items_essences_are_actual_boundary_deltas(self):
        before = self.engine()
        before.s.items = ["worker"]
        after = self.clone(before)
        after.s.gold += 10
        after.s.tokens.update(remove=2, roll=3)
        after.s.items = ["magic_filter"]
        essence = next(iter(after.catalog.essences))
        after.s.consumed_essences.append(essence)
        changes = action_changes(before, after)
        self.assertEqual(10, changes["gold"]["delta"])
        self.assertEqual(2, changes["tokens"]["remove"]["delta"])
        self.assertEqual(3, changes["tokens"]["roll"]["delta"])
        self.assertEqual([("added", "magic_filter"), ("removed", "worker")], [(x["kind"], x["id"]) for x in changes["items"]])
        self.assertEqual(essence, changes["consumed_essences"][0]["id"])
        self.assertEqual(1, changes["consumed_essences"][0]["count"])

    def test_summary_is_pure_repeatable_and_not_a_same_action_lifecycle_trace(self):
        before = self.engine()
        after = self.clone(before)
        transient = after.add_ingredient("water", emit=False)
        after._remove(transient, "manual", None)
        states = deepcopy(before.s.to_dict()), deepcopy(after.s.to_dict()), before.r.state, after.r.state
        one, two = action_changes(before, after), action_changes(before, after)
        self.assertEqual(one, two)
        self.assertEqual([], one["ingredients"])
        self.assertEqual(states, (before.s.to_dict(), after.s.to_dict(), before.r.state, after.r.state))

    def test_bridge_summary_does_not_change_game_save_rng_or_status_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.json"
            bridge = DesktopBridge(path)
            bridge.new_game(20261019)
            core = GameEngine()
            core.new_game(20261019)
            core.spin()
            view = bridge.action("end_turn")
            self.assertTrue(view["ok"])
            self.assertEqual(core.s.to_dict(), bridge._engine.s.to_dict())
            self.assertEqual(core.r.state, bridge._engine.r.state)
            self.assertEqual(core.s.to_dict(), load_game(path).to_dict())
            self.assertIn("action_changes", view)
            self.assertNotIn("action_changes", bridge.get_state())
            self.assertNotIn("action_changes", bridge._engine.s.to_dict())
            before = deepcopy(bridge._engine.s.to_dict()), path.read_bytes()
            with patch("desktop.bridge.save_game", side_effect=OSError("disk full")):
                failed = bridge.action("choose", {"number": 1})
            self.assertFalse(failed["ok"])
            self.assertNotIn("action_changes", failed)
            self.assertEqual(before, (bridge._engine.s.to_dict(), path.read_bytes()))
