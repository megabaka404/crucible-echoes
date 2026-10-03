from copy import deepcopy
import unittest

from crucible_echoes.cli import apply_action
from crucible_echoes.engine import GameEngine, GameError
from crucible_echoes.model import PendingChoice


class ActionWindowAndDepartedEffectsTests(unittest.TestCase):
    def board(self, *ids):
        engine = GameEngine()
        engine.new_game(42)
        engine.s.ingredients.clear()
        for name in ids:
            engine.add_ingredient(name, emit=False)
        engine._board = list(engine.s.ingredients)
        engine._coords = [(0, i) for i in range(len(ids))]
        engine._values = [engine.stable_ingredient_value(x) for x in engine._board]
        return engine

    def test_departed_expert_does_not_protect_equipment(self):
        engine = self.board("lab_expert", "test_tube")
        expert, tube = engine._board
        self.assertFalse(engine._remove(tube, "shattered", 1))
        self.assertTrue(engine._remove(expert, "manual", 0))
        self.assertTrue(engine._remove(tube, "shattered", 1))
        self.assertEqual(1, engine._round_events["shattered"])

    def test_public_spin_does_not_use_departed_expert_protection(self):
        engine = self.board("butcher", "lab_expert", "test_tube", "apprentice")
        expert, tube = engine._board[1:3]
        engine.r.sample = lambda rows, count: list(rows)[:count]
        engine.r.random = lambda: 0.0
        engine.spin()
        self.assertFalse(engine._present(expert))
        self.assertFalse(engine._present(tube))

    def test_dead_proliferation_core_cannot_grow_or_pay_echo(self):
        engine = self.board("summon_magic", "proliferation_core")
        engine.s.items.append("reaction_echo")
        core = engine._board[1]
        engine._remove(core, "manual", 1)
        before = engine.s.gold
        engine._spawn_random(def_id="water", source=0, origin="ingredient")
        self.assertEqual(0, core.permanent_bonus)
        self.assertEqual(before, engine.s.gold)
        self.assertEqual(0, engine._round_events["permanent_bonus"])
        self.assertNotIn(f"proliferated:{engine.s.spin}", core.flags)

    def test_live_proliferation_still_grows_once_without_extra_random_draws(self):
        engine = self.board("summon_magic", "proliferation_core")
        core = engine._board[1]
        before = engine.r.state
        for _ in range(2):
            engine._spawn_random(def_id="water", source=0, origin="ingredient")
        self.assertEqual(1, core.permanent_bonus)
        self.assertEqual(before, engine.r.state)
        engine.s.spin += 1
        engine._spawn_random(def_id="water", source=0, origin="ingredient")
        self.assertEqual(2, core.permanent_bonus)

    def test_generic_permanent_growth_rejects_detached_instances(self):
        engine = self.board("water")
        target = engine._board[0]
        engine._remove(target, "manual", 0)
        engine.s.items.extend(["reaction_echo", "experiment_notebook"])
        before = deepcopy(engine.s.to_dict()), engine.r.state
        engine._permanent_bonus(target, 3)
        engine._permanent_bonus(target, -1)
        self.assertEqual(0, target.permanent_bonus)
        self.assertEqual(before, (engine.s.to_dict(), engine.r.state))

    def test_all_public_mutations_reject_terminal_states_without_changes(self):
        for status in ("won", "lost"):
            for command, args in (("spin", []), ("choose", ["1"]), ("skip", []), ("reroll", []),
                                  ("remove", ["1"]), ("use", ["sandpaper_box"]), ("toggle", ["ban"])):
                with self.subTest(status=status, command=command):
                    engine = self.board("water")
                    engine.s.items.extend(["sandpaper_box", "ban"])
                    engine.s.tokens.update(roll=2, remove=2)
                    engine.s.pending = [PendingChoice("ingredient", ["water"])] if command in {"choose", "skip", "reroll"} else []
                    engine.s.status = status
                    before = deepcopy(engine.s.to_dict()), engine.r.state
                    with self.assertRaisesRegex(GameError, "已经结束"):
                        apply_action(engine, command, args)
                    self.assertEqual(before, (engine.s.to_dict(), engine.r.state))

    def test_pending_rewards_block_active_items_before_any_effect(self):
        for command, args in (("use", ["sandpaper_box"]), ("toggle", ["ban"]), ("remove", ["1"]), ("spin", [])):
            engine = self.board("water")
            engine.s.items.extend(["sandpaper_box", "ban"])
            engine.s.tokens["remove"] = 2
            engine.s.pending = [PendingChoice("ingredient", ["water"])]
            before = deepcopy(engine.s.to_dict()), engine.r.state
            with self.assertRaisesRegex(GameError, "处理当前选择"):
                apply_action(engine, command, args)
            self.assertEqual(before, (engine.s.to_dict(), engine.r.state))


if __name__ == "__main__":
    unittest.main()
