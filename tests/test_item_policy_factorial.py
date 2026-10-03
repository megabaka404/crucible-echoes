from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from crucible_echoes.action_strategy import ContentActionStrategy
from crucible_echoes.engine import GameEngine
from crucible_echoes.item_strategy import ItemEconomyStrategy
from crucible_echoes.model import PendingChoice
from crucible_echoes.simulation import simulate_game, run_batch
from crucible_echoes.strategy_benchmark import compact_report
from tools.item_policy_factorial import ItemPolicyFactorial, run_experiment, validate_cell


class ItemPolicyFactorialTests(unittest.TestCase):
    def engine(self):
        engine = GameEngine()
        engine.new_game(20261032)
        engine.s.spin = 5
        engine.s.spins_left = 5
        engine.s.tokens['roll'] = 1
        engine.s.stats['event_counts'] = {'order_completed': 2}
        return engine

    def test_reroll_uses_independent_bound_receiver_and_preserves_state_rng(self):
        engine = self.engine()
        choice = PendingChoice('item', ['cat_litter', 'pink_reagent', 'old_ledger'])
        before = deepcopy(engine.s.to_dict())
        self.assertTrue(ContentActionStrategy().should_reroll(engine, choice))
        self.assertFalse(ItemEconomyStrategy().should_reroll(engine, choice))
        # Calling the parent method on a v4 receiver is NOT a v3 ablation.
        self.assertFalse(ContentActionStrategy.should_reroll(ItemEconomyStrategy(), engine, choice))
        for pick in (3, 4):
            self.assertTrue(ItemPolicyFactorial(pick, 3).should_reroll(engine, choice))
            self.assertFalse(ItemPolicyFactorial(pick, 4).should_reroll(engine, choice))
        self.assertEqual(before, engine.s.to_dict())

    def test_choice_scoring_is_independent_of_reroll_factor(self):
        engine = self.engine()
        choice = PendingChoice('item', ['cat_litter', 'pink_reagent', 'old_ledger'])
        for pick, baseline in ((3, ContentActionStrategy()), (4, ItemEconomyStrategy())):
            for roll in (3, 4):
                policy = ItemPolicyFactorial(pick, roll)
                self.assertIsNot(policy.pick_policy, policy.roll_policy)
                self.assertEqual(baseline.choose(engine, choice), policy.choose(engine, choice))
                for item in choice.offers:
                    self.assertEqual(baseline.score_components(engine, 'item', item),
                                     policy.score_components(engine, 'item', item))

    def test_nonitem_decisions_always_use_v3(self):
        engine = self.engine()
        baseline = ContentActionStrategy()
        choice = PendingChoice('ingredient', ['paper', 'cat', 'copper_ore'])
        # Use actual catalog IDs to avoid accidentally testing a nonexistent fixture.
        choice.offers = list(engine.catalog.ingredients)[:3]
        for pick in (3, 4):
            for roll in (3, 4):
                policy = ItemPolicyFactorial(pick, roll)
                self.assertEqual(baseline.choose(engine, choice), policy.choose(engine, choice))
                self.assertEqual(baseline.should_reroll(engine, choice), policy.should_reroll(engine, choice))
                self.assertEqual(baseline.removal_index(engine), policy.removal_index(engine))
                self.assertEqual(baseline.pre_spin_action(engine), policy.pre_spin_action(engine))
                for ingredient in choice.offers:
                    self.assertEqual(baseline.score_components(engine, 'ingredient', ingredient),
                                     policy.score_components(engine, 'ingredient', ingredient))

    def test_diagonal_cells_reproduce_entire_registered_policy_records(self):
        for version, factory in ((3, ContentActionStrategy), (4, ItemEconomyStrategy)):
            for difficulty in (7, 15):
                seed = 20261033 + difficulty
                with self.subTest(version=version, difficulty=difficulty):
                    original = simulate_game(seed, difficulty, strategy=factory()).to_dict()
                    treatment = simulate_game(seed, difficulty,
                        strategy=ItemPolicyFactorial(version, version)).to_dict()
                    self.assertEqual(original, treatment)
                    self.assertNotEqual('aborted', original['status'])

    def test_resume_reuses_partial_cells_and_refuses_changed_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = dict(games=1, seed=20261033, difficulties=[7], chunk_size=1,
                        output=Path(tmp) / 'factorial.json')
            report = run_experiment(**args, max_cells=2)
            self.assertFalse(report['complete'])
            self.assertEqual({}, report['difficulties'])  # No unequal-subset comparisons.
            with patch('tools.item_policy_factorial.run_batch', wraps=run_batch) as runner:
                complete = run_experiment(**args, resume=True)
                self.assertEqual(2, runner.call_count)
            self.assertTrue(complete['complete'])
            self.assertEqual(1, complete['difficulties']['7']['games'])
            with patch('tools.item_policy_factorial.run_batch', side_effect=AssertionError('replayed')):
                self.assertEqual(complete, run_experiment(**args, resume=True))
            with self.assertRaises(ValueError):
                run_experiment(**{**args, 'seed': 99}, resume=True)
            with self.assertRaises(ValueError):
                run_experiment(**args)

    def test_checkpoint_corruption_and_invalid_versions_rejected(self):
        part = compact_report(run_batch(1, 42, 7, strategy=ItemPolicyFactorial()))
        args = dict(seed=42, difficulty=7, start=0, count=1, cell='pick3_roll3')
        validate_cell(part, **args)
        for field in ('seed', 'aborted', 'wins', 'status', 'count'):
            damaged = deepcopy(part)
            if field == 'seed': damaged['games'][0]['seed'] += 1
            elif field == 'aborted': damaged['summary']['aborted'] = 1
            elif field == 'wins': damaged['summary']['wins'] += 1
            elif field == 'status': damaged['games'][0]['status'] = 'playing'
            else: damaged['games'].append(deepcopy(damaged['games'][0]))
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_cell(damaged, **args)
        with self.assertRaises(ValueError):
            ItemPolicyFactorial(5, 3)


if __name__ == '__main__':
    unittest.main()
