from copy import deepcopy
import unittest

from tools.paired_item_trace import common_decision_changes, discordant_pairs, observe_before_commit, observe_reroll_before_commit
from tools.paired_item_trace import publish_report


class PairedItemTraceTests(unittest.TestCase):
    def test_transient_publication_retries_and_is_bounded(self):
        from unittest.mock import Mock, patch
        writer = Mock(side_effect=[PermissionError(), PermissionError(), None])
        with patch('tools.paired_item_trace.time.sleep') as sleep:
            publish_report('result', {}, writer)
        self.assertEqual(3, writer.call_count)
        self.assertEqual(2, sleep.call_count)
        writer = Mock(side_effect=PermissionError())
        with patch('tools.paired_item_trace.time.sleep'), self.assertRaises(PermissionError):
            publish_report('result', {}, writer)
        self.assertEqual(5, writer.call_count)

    def test_nonsharing_publication_errors_are_not_hidden_or_retried(self):
        from unittest.mock import Mock
        writer = Mock(side_effect=ValueError('bad report'))
        with self.assertRaises(ValueError):
            publish_report('result', {}, writer)
        self.assertEqual(1, writer.call_count)

    def chunk(self):
        return {'range': {'difficulty': 10, 'count': 2},
                'baseline': {'games': [{'index': 0, 'seed': 7, 'status': 'won', 'won': True},
                                      {'index': 1, 'seed': 8, 'status': 'lost', 'won': False}]},
                'candidate': {'games': [{'index': 0, 'seed': 7, 'status': 'lost', 'won': False},
                                       {'index': 1, 'seed': 8, 'status': 'lost', 'won': False}]}}

    def test_only_valid_discordant_games_are_selected_without_mutation(self):
        chunk = self.chunk()
        before = deepcopy(chunk)
        self.assertEqual([{'index': 0, 'seed': 7, 'baseline_won': True, 'candidate_won': False}],
                         discordant_pairs([chunk], 10))
        self.assertEqual(before, chunk)

    def test_mismatch_duplicate_abort_and_missing_count_are_rejected(self):
        for mutation in ('seed', 'abort', 'count', 'duplicate'):
            chunks = [self.chunk()]
            if mutation == 'seed': chunks[0]['candidate']['games'][0]['seed'] = 99
            elif mutation == 'abort': chunks[0]['candidate']['games'][0]['status'] = 'aborted'
            elif mutation == 'count': chunks[0]['range']['count'] = 3
            else: chunks.append(deepcopy(chunks[0]))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                discordant_pairs(chunks, 10)

    def test_only_identical_states_support_decision_comparison(self):
        old = [{'state_hash': 'same', 'selected': 'a'}]
        new = [{'state_hash': 'different', 'selected': 'b', 'spin': 7, 'order': 2, 'offers': []},
               {'state_hash': 'same', 'selected': 'b', 'spin': 6, 'order': 2, 'offers': []}]
        self.assertEqual([{'spin': 6, 'order': 2, 'baseline_selected': 'a',
                          'candidate_selected': 'b', 'offers': []}], common_decision_changes(old, new))

    def test_observer_sees_precommit_inventory_and_original_return_value(self):
        from crucible_echoes.engine import GameEngine
        from crucible_echoes.model import PendingChoice
        from types import SimpleNamespace
        for result, can_skip, selected in ((1, True, 'worker'), (None, True, None), (None, False, 'worker')):
            with self.subTest(result=result, can_skip=can_skip):
                engine = GameEngine()
                engine.new_game(77)
                choice = PendingChoice(kind='item', offers=['worker'], can_skip=can_skip)
                engine.s.pending = [choice]
                calls = []
                def choose(e, c):
                    calls.append('choose')
                    return result
                policy = SimpleNamespace(choose=choose)
                observed = []
                observe_before_commit(policy, lambda e, c, key: observed.append((list(e.s.items), key)))
                self.assertEqual(result, policy.choose(engine, choice))
                self.assertEqual([([], selected)], observed)
                self.assertEqual(['choose'], calls)
                if selected:
                    engine.choose(1)
                    self.assertIn('worker', engine.s.items)
                else:
                    engine.skip()

    def test_reroll_observer_preserves_decision_and_does_not_spend_tokens(self):
        from types import SimpleNamespace
        for decision in (False, True):
            engine = SimpleNamespace(tokens=3)
            calls = []
            def should_reroll(e, c):
                calls.append('should_reroll')
                return decision
            policy = SimpleNamespace(should_reroll=should_reroll)
            observed = []
            observe_reroll_before_commit(policy, lambda e, c, d: observed.append((e.tokens, d)))
            self.assertIs(decision, policy.should_reroll(engine, None))
            self.assertEqual([(3, decision)], observed)
            self.assertEqual(['should_reroll'], calls)
            self.assertEqual(3, engine.tokens)


if __name__ == '__main__':
    unittest.main()
