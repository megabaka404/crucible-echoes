"""Compare uninterrupted and one-process-per-action public game trajectories."""
from __future__ import annotations

import json
import random
from collections import defaultdict
import unittest

from crucible_echoes.engine import GameEngine
from crucible_echoes.model import GameState


class ResumeTrajectoryTests(unittest.TestCase):
    def restored(self, engine):
        return GameEngine(engine.catalog).bind(GameState.from_dict(json.loads(json.dumps(engine.s.to_dict()))))

    def apply(self, engine, action):
        command = action["action"]
        if command in {"choose", "remove"}:
            getattr(engine, command)(action["index"])
        elif command == "toggle":
            engine.toggle_item(action["item_id"])
        elif command == "use":
            engine.use_item(action["item_id"])
        else:
            getattr(engine, command)()

    def test_consumed_choice_guarantees_keep_canonical_zero_state(self):
        for command in ("choose", "skip"):
            with self.subTest(command=command):
                engine = GameEngine()
                engine.new_game(20261023)
                engine.s.flags.update(choice_minimum_count=2, choice_minimum_rarity=3)
                engine.s.pending.append(engine.make_choice("ingredient"))
                self.apply(engine, {"action": command, "index": 1})
                self.assertEqual(1, engine.s.flags["choice_minimum_count"])
                self.assertEqual(0, engine.s.flags.get("choice_minimum_reserved", 0))
                self.assertEqual(engine.agent_payload("status"), self.restored(engine).agent_payload("status"))
                engine.s.pending.append(engine.make_choice("ingredient"))
                self.assertGreaterEqual(engine.catalog.ingredients[engine.s.pending[0].offers[0]]["rarity"], 3)
                self.apply(engine, {"action": command, "index": 1})
                self.assertEqual({"choice_minimum_count": 0, "choice_minimum_rarity": 0,
                                  "choice_minimum_reserved": 0},
                                 {key: engine.s.flags.get(key) for key in
                                  ("choice_minimum_count", "choice_minimum_rarity", "choice_minimum_reserved")})
                rng = engine.r.state
                restored = self.restored(engine)
                self.assertEqual(engine.s.to_dict(), restored.s.to_dict())
                self.assertEqual(rng, restored.r.state)

    def test_legacy_absent_guarantee_flags_bind_to_zero_without_rng(self):
        engine = GameEngine()
        engine.new_game(20261023)
        for key in ("choice_minimum_count", "choice_minimum_rarity", "choice_minimum_reserved"):
            engine.s.flags.pop(key, None)
        rng = engine.r.state
        restored = self.restored(engine)
        for key in ("choice_minimum_count", "choice_minimum_rarity", "choice_minimum_reserved"):
            self.assertEqual(0, restored.s.flags[key])
        self.assertEqual(rng, restored.r.state)

    def test_seeded_mixed_legal_actions_match_after_each_reload(self):
        # This is a stress fixture, not a player policy or balance scan.
        # A separate test RNG picks legal commands; game RNG is never used
        # by the scheduler. Definitions are selected by mechanisms, not IDs.
        exercised = set()
        weights = {"spin": 6, "choose": 5, "skip": 2, "reroll": 2,
                   "remove": 1, "use": 3, "toggle": 1}
        fields = ("chance_spawn", "periodic_spawn", "spawn_each_spin", "remove_after",
                  "transform_after", "potion", "periodic_adjacent_permanent_growth")
        for seed in (20261021, 20261022):
            for mode_index, mode in enumerate(("none", "giant", "rapid", "blind_box", "minimal", "mutation")):
                for difficulty in (7, 15):
                    with self.subTest(seed=seed, mode=mode, difficulty=difficulty):
                        live = GameEngine()
                        live.new_game(seed, difficulty, mode)
                        live.s.gold = 20000  # Keep stress fixtures alive, not a game rule.
                        live.s.tokens.update(roll=10, remove=10, essence=3)
                        for row in live.catalog.items.values():
                            if row.get("active") or row.get("toggle_flag") or row.get("protect_once"):
                                live.add_item(row["id"])
                        for field in fields:
                            row = next((row for row in live.catalog.ingredients.values()
                                        if row.get(field) and row.get("offerable", True)), None)
                            if row:
                                live.add_ingredient(row["id"], emit=False)
                        for row in live.catalog.essences.values():
                            trigger = row.get("trigger", {})
                            if trigger.get("event_count_round") or trigger.get("event_value"):
                                live.add_essence(row["id"])
                        live._sync_rng()
                        resumed = self.restored(live)
                        scheduler = random.Random(seed * 31 + difficulty + mode_index * 100)
                        for step in range(160):
                            self.assertEqual(live.s.to_dict(), resumed.s.to_dict())
                            if live.s.status != "playing":
                                break
                            specs = live.agent_action_specs()
                            self.assertEqual(specs, resumed.agent_action_specs())
                            groups = defaultdict(list)
                            for spec in specs:
                                if spec["action"] in weights:
                                    groups[spec["action"]].append(spec)
                            kinds = sorted(groups)
                            self.assertTrue(kinds, "Playing fixture has no legal mutating action")
                            kind = scheduler.choices(kinds, weights=[weights[x] for x in kinds], k=1)[0]
                            action = scheduler.choice(groups[kind])
                            exercised.add(kind)
                            self.apply(live, action)
                            self.apply(resumed, action)
                            context = (seed, mode, difficulty, step, action)
                            self.assertEqual(live.s.to_dict(), resumed.s.to_dict(), context)
                            self.assertEqual(live.r.state, resumed.r.state, context)
                            self.assertGreaterEqual(live.s.gold, 0)
                            self.assertTrue(all(value >= 0 for value in live.s.tokens.values()))
                            resumed = self.restored(resumed)
                            self.assertEqual(live.agent_payload("status"), resumed.agent_payload("status"), context)
        self.assertEqual(set(weights), exercised)

    def test_every_action_reload_matches_all_modes_and_high_difficulties(self):
        for mode in ("none", "giant", "rapid", "blind_box", "minimal", "mutation"):
            for difficulty in (7, 15):
                with self.subTest(mode=mode, difficulty=difficulty):
                    live = GameEngine()
                    live.new_game(20261006, difficulty, fun_mode=mode)
                    live.s.gold = 10000  # Keep the fixture alive to exercise later rounds.
                    live.s.tokens.update(roll=6, remove=3)
                    live.add_item("lucky_compass")
                    live.add_item("ban")
                    live.add_item("large_material_pack")
                    live.add_essence("auto_reroller_essence")
                    live.add_essence("frozen_meal_essence")
                    live._sync_rng()
                    resumed = self.restored(live)
                    used = set()
                    actions = 0
                    while live.s.status == "playing" and live.s.spin < 12:
                        self.assertLess(actions, 200, "Public action fixture made no progress")
                        specs = live.agent_action_specs()
                        self.assertEqual(specs, resumed.agent_action_specs())
                        if live.s.pending:
                            choice = live.s.pending[0]
                            if any(x["action"] == "reroll" for x in specs) and actions % 7 == 0:
                                action = {"action": "reroll"}
                            elif choice.can_skip and actions % 5 == 0:
                                action = {"action": "skip"}
                            else:
                                action = {"action": "choose", "index": 1}
                        else:
                            active = next((x for x in specs if x["action"] == "use" and x["item_id"] not in used), None)
                            toggle_key = ("toggle", live.s.spin)
                            remove_key = ("remove", live.s.spin)
                            if active:
                                action = active
                                used.add(active["item_id"])
                            elif live.s.spin in (2, 4) and toggle_key not in used:
                                action = next(x for x in specs if x["action"] == "toggle")
                                used.add(toggle_key)
                            elif live.s.spin in (3, 6, 9) and remove_key not in used and any(x["action"] == "remove" for x in specs):
                                action = next(x for x in specs if x["action"] == "remove")
                                used.add(remove_key)
                            else:
                                action = {"action": "spin"}
                        self.apply(live, action)
                        self.apply(resumed, action)
                        self.assertEqual(live.s.to_dict(), resumed.s.to_dict(), (mode, difficulty, actions, action))
                        self.assertEqual(live.r.state, resumed.r.state)
                        resumed = self.restored(resumed)
                        actions += 1
                    self.assertEqual(12, live.s.spin)


if __name__ == "__main__":
    unittest.main()
