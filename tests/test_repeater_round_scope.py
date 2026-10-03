from copy import deepcopy
import unittest

from crucible_echoes.engine import GameEngine, GameError
from crucible_echoes.model import GameState, PendingChoice


class RepeaterRoundScopeTests(unittest.TestCase):
    def fresh(self):
        engine = GameEngine()
        engine.new_game(20261014)
        engine.s.ingredients.clear()
        engine.s.pending.clear()
        engine.s.gold = 100
        repeater = engine.add_ingredient("repeater", emit=False)
        engine._board = [repeater]
        engine._coords = [(0, 0)]
        engine._values = [0]
        return engine, repeater

    def test_same_round_payload_repeats_once_and_does_not_use_rng(self):
        engine, repeater = self.fresh()
        potion = engine.add_ingredient("wealth_potion", emit=False)
        engine._board.append(potion)
        engine._coords.append((0, 1))
        engine._values.append(0)
        before = engine.r.state
        engine._trigger_potion(1, potion, {"gold": 10})
        engine._run_script(0, repeater, "repeater")
        engine._run_script(0, repeater, "repeater")
        self.assertEqual(120, engine.s.gold)
        self.assertEqual(before, engine.r.state)

    def test_old_saved_payload_cannot_reward_a_later_round(self):
        engine, _ = self.fresh()
        engine.s.stats["last_potion"] = {"gold": 999}
        saved = GameState.from_dict(deepcopy(engine.s.to_dict()))
        engine.bind(saved)
        reference, _ = self.fresh()
        self.assertEqual(reference.r.state, engine.r.state)
        engine.spin()
        reference.spin()
        self.assertEqual(reference.s.gold, engine.s.gold)
        self.assertEqual(reference.r.state, engine.r.state)
        self.assertNotIn("last_potion", engine.s.stats)

    def test_illegal_spin_does_not_clear_current_round_memory(self):
        engine, _ = self.fresh()
        engine.s.stats["last_potion"] = {"gold": 10}
        engine.s.pending = [PendingChoice("ingredient", ["water"])]
        before = deepcopy(engine.s.to_dict())
        with self.assertRaises(GameError):
            engine.spin()
        self.assertEqual(before, engine.s.to_dict())
