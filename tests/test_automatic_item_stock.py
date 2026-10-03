from copy import deepcopy
import unittest

from crucible_echoes.engine import GameEngine
from crucible_echoes.model import GameState


class AutomaticItemStockTests(unittest.TestCase):
    def fresh(self, ingredient, *, all_owned=True):
        engine = GameEngine()
        engine.new_game(20261029)
        engine.s.ingredients.clear()
        engine.s.pending.clear()
        engine.s.items = [row['id'] for row in engine.catalog.items.values()
                          if all_owned or row['rarity'] == 1]
        instance = engine.add_ingredient(ingredient, emit=False)
        engine._board = [instance]
        engine._coords = [(0, 0)]
        engine._values = [0]
        engine.s.tokens['remove'] = 1
        return engine, instance

    def test_undead_public_delete_completes_when_all_items_are_owned(self):
        engine, instance = self.fresh('undead')
        rng = engine.r.state
        engine.remove(1)
        self.assertFalse(engine.s.ingredients)
        self.assertEqual(0, engine.s.tokens['remove'])
        self.assertEqual(1, engine.s.stats['event_counts']['removed'])
        self.assertEqual(1, engine.s.stats['event_counts']['manual_removed'])
        self.assertEqual(rng, engine.r.state)
        restored = GameEngine().bind(GameState.from_dict(engine.s.to_dict()))
        self.assertEqual(engine.s.to_dict(), restored.s.to_dict())

    def test_fixed_item_removal_reward_does_not_upgrade_when_tier_is_empty(self):
        engine, instance = self.fresh('undead', all_owned=False)
        items = list(engine.s.items)
        engine.remove(1)
        self.assertEqual(items, engine.s.items)

    def test_demon_empty_random_reward_does_not_draw_rng_or_interrupt_removal(self):
        engine, instance = self.fresh('demon')
        rng = engine.r.state
        engine.remove(1)
        self.assertFalse(engine.s.ingredients)
        self.assertEqual(rng, engine.r.state)

    def test_universal_chest_keeps_other_rewards_when_item_tier_is_empty(self):
        engine, instance = self.fresh('universal_chest')
        before = dict(engine.s.tokens)
        rng = engine.r.state
        self.assertTrue(engine._remove(instance, 'opened', 0))
        self.assertFalse(engine.s.ingredients)
        for kind in ('remove', 'roll', 'essence'):
            self.assertEqual(before[kind] + 1, engine.s.tokens[kind])
        self.assertEqual(rng, engine.r.state)

    def test_merchant_empty_tier_keeps_documented_cost_without_upgrade(self):
        engine, instance = self.fresh('merchant', all_owned=False)
        instance.age = 10
        engine.s.gold = 100
        items = list(engine.s.items)
        rng = engine.r.state
        engine._run_script(0, instance, 'merchant')
        self.assertEqual(95, engine.s.gold)
        self.assertEqual(items, engine.s.items)
        self.assertEqual(rng, engine.r.state)

    def test_stocked_random_item_draw_preserves_historical_rng(self):
        for seed in range(20):
            engine = GameEngine()
            engine.new_game(seed)
            engine.s.order_index = 8
            engine._sync_rng()
            reference = GameEngine().bind(GameState.from_dict(deepcopy(engine.s.to_dict())))
            expected = reference._draw_definition('item', reference.roll_rarity('item'))
            actual = engine._draw_available_item(None)
            self.assertEqual(expected, actual)
            self.assertEqual(reference.r.state, engine.r.state)


if __name__ == '__main__':
    unittest.main()
