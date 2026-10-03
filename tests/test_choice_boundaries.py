from copy import deepcopy
import unittest

from crucible_echoes.engine import GameEngine
from crucible_echoes.model import GameState


class ChoiceBoundaryTests(unittest.TestCase):
    def fresh(self, seed=2):
        engine = GameEngine()
        engine.new_game(seed)
        engine.s.order_index = 8
        return engine

    def test_minimum_reward_falls_upward_when_requested_tier_is_owned(self):
        for seed in range(40):
            with self.subTest(seed=seed):
                engine = self.fresh(seed)
                engine.s.items = [row['id'] for row in engine.catalog.items.values() if row['rarity'] == 3]
                choice = engine.make_choice('item', minimums=[3])
                self.assertGreaterEqual(engine.catalog.items[choice.offers[0]]['rarity'], 3)

    def test_fixed_reward_only_uses_requested_tier_and_can_exhaust(self):
        engine = self.fresh()
        remaining = [row['id'] for row in engine.catalog.items.values() if row['rarity'] == 3][:2]
        engine.s.items = [row['id'] for row in engine.catalog.items.values() if row['rarity'] == 3 and row['id'] not in remaining]
        choice = engine.make_choice('item', fixed_rarity=3)
        self.assertEqual(set(remaining), set(choice.offers))
        engine.s.items.extend(remaining)
        before = engine.r.state
        choice = engine.make_choice('item', fixed_rarity=3, can_skip=False)
        self.assertEqual([], choice.offers)
        self.assertTrue(choice.can_skip)
        self.assertEqual(before, engine.r.state)

    def test_membership_discarded_trials_do_not_award_choice_events(self):
        engine = GameEngine()
        engine.new_game(2)
        engine.s.items = ['lab_membership', 'probability_calibrator']
        engine.s.pending.append(engine.make_choice('ingredient'))
        engine.s.tokens['roll'] = 1
        engine._sync_rng()
        reference = GameEngine().bind(GameState.from_dict(deepcopy(engine.s.to_dict())))
        old = set(reference.s.pending[0].offers)
        expected = reference.make_choice('ingredient', _redraw=True)
        attempts = 0
        while old.intersection(expected.offers) and attempts < 20:
            expected = reference.make_choice('ingredient', _redraw=True)
            attempts += 1
        self.assertGreater(attempts, 0)
        gold = engine.s.gold
        events = engine.s.stats['event_counts'].get('all_common_choice', 0)
        engine.reroll()
        self.assertEqual(expected.offers, engine.s.pending[0].offers)
        self.assertEqual(reference.r.state, engine.r.state)
        self.assertEqual(1, engine.s.stats['event_counts'].get('all_common_choice', 0) - events)
        self.assertEqual(5, engine.s.gold - gold)

    def test_minimum_constraints_survive_reroll_and_reload(self):
        engine = self.fresh()
        engine.s.items = [row['id'] for row in engine.catalog.items.values() if row['rarity'] == 3]
        engine.s.pending.append(engine.make_choice('item', minimums=[3]))
        engine.s.tokens['roll'] = 2
        engine._sync_rng()
        for _ in range(2):
            engine = GameEngine().bind(GameState.from_dict(engine.s.to_dict()))
            engine.reroll()
            self.assertGreaterEqual(engine.catalog.items[engine.s.pending[0].offers[0]]['rarity'], 3)
            self.assertEqual([3], engine.s.pending[0].details['draw_constraints']['minimums'])

    def test_minimum_pool_exhaustion_does_not_substitute_lower_tiers(self):
        engine = self.fresh()
        remaining = next(row['id'] for row in engine.catalog.items.values() if row['rarity'] == 4)
        engine.s.items = [row['id'] for row in engine.catalog.items.values() if row['rarity'] >= 3 and row['id'] != remaining]
        choice = engine.make_choice('item', minimums=[3, 3, 3])
        self.assertEqual([remaining], choice.offers)
        engine.s.items.append(remaining)
        before = engine.r.state
        choice = engine.make_choice('item', minimums=[3], can_skip=False)
        self.assertEqual([], choice.offers)
        self.assertTrue(choice.can_skip)
        self.assertEqual(before, engine.r.state)

    def test_generated_minimum_does_not_fall_below_bound_in_sparse_family(self):
        engine = self.fresh()
        # Generic sparse-family fixture: the rolled tier has no definition,
        # but both a lower and a higher tier are available.
        low = next(row for row in engine.catalog.ingredients.values() if row['rarity'] == 1 and row.get('offerable', True))
        high = next(row for row in engine.catalog.ingredients.values() if row['rarity'] == 4 and row.get('offerable', True) and not row.get('unique'))
        low.setdefault('tags', []).append('boundary_fixture')
        high.setdefault('tags', []).append('boundary_fixture')
        created = engine._spawn_random(tag='boundary_fixture', rarity=3, minimum_rarity=3)
        self.assertEqual(high['id'], created.def_id)


if __name__ == '__main__':
    unittest.main()
