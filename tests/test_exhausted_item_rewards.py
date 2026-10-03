from copy import deepcopy
import unittest

from crucible_echoes.engine import GameEngine, GameError
from crucible_echoes.model import GameState


class ExhaustedItemRewardTests(unittest.TestCase):
    def fresh(self, remaining=()):
        engine = GameEngine()
        engine.new_game(20261026)
        engine.s.items = [key for key in engine.catalog.items if key not in remaining]
        return engine

    def test_two_remaining_items_make_two_candidates_without_duplicates(self):
        engine = self.fresh(("worker", "coin_pouch"))
        choice = engine.make_choice("item", source="order")
        self.assertEqual({"worker", "coin_pouch"}, set(choice.offers))
        self.assertEqual(2, len(choice.offers))

    def test_exhausted_item_pool_is_empty_skippable_reward_without_rng_draws(self):
        engine = self.fresh()
        rng = engine.r.state
        engine.s.pending.append(engine.make_choice("item", source="order"))
        engine.s.tokens["roll"] = 2
        engine._sync_rng()
        self.assertEqual([], engine.s.pending[0].offers)
        self.assertIn("skip", engine.agent_available_actions())
        self.assertNotIn("reroll", engine.agent_available_actions())
        self.assertFalse(any(row["action"] in {"choose", "reroll"} for row in engine.agent_action_specs()))
        before = deepcopy(engine.s.to_dict())
        with self.assertRaises(GameError):
            engine.reroll()
        self.assertEqual(before, engine.s.to_dict())
        self.assertEqual(rng, engine.r.state)
        restored = GameEngine().bind(GameState.from_dict(engine.s.to_dict()))
        engine.skip(); restored.skip()
        self.assertEqual(engine.s.to_dict(), restored.s.to_dict())
        self.assertIn("spin", restored.agent_available_actions())

    def test_peace_order_completes_with_all_items_owned_and_reward_queue_resolves(self):
        engine = self.fresh()
        engine.s.peace_mode = True
        engine.s.peace_order = 0
        engine.s.spins_left = 1
        engine.spin()
        self.assertEqual("playing", engine.s.status)
        self.assertEqual(1, engine.s.peace_order)
        self.assertEqual(7, engine.s.spins_left)
        empty = [choice for choice in engine.s.pending if choice.kind == "item"]
        self.assertTrue(empty)
        self.assertTrue(all(not choice.offers and choice.can_skip for choice in empty))
        for _ in range(100):
            if not engine.s.pending:
                break
            engine.skip()
        self.assertFalse(engine.s.pending)
        self.assertIn("spin", engine.agent_available_actions())

    def test_empty_forced_item_reward_does_not_create_an_unresolvable_queue(self):
        engine = self.fresh()
        choice = engine.make_choice("item", can_skip=False)
        self.assertEqual([], choice.offers)
        self.assertTrue(choice.can_skip)


if __name__ == "__main__":
    unittest.main()
