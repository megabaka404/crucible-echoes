from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from crucible_echoes.model import PendingChoice
from crucible_echoes.save import save_game
from desktop.bridge import DesktopBridge


class DesktopIntegerInputTests(unittest.TestCase):
    invalid = (True, False, 1.9, 1.0, float('inf'), None, '1.9', '1e0', '1_0')

    def snapshot(self, bridge):
        return id(bridge._engine), deepcopy(bridge._engine.s.to_dict()), bridge._engine.r.state, bridge._save_path.read_bytes()

    def test_invalid_difficulty_cannot_silently_replace_saved_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = DesktopBridge(Path(tmp) / 'current.json')
            for value in self.invalid:
                with self.subTest(value=value):
                    bridge.new_game(42, 7)
                    before = self.snapshot(bridge)
                    self.assertFalse(bridge.new_game(99, value)['ok'])
                    self.assertEqual(before, self.snapshot(bridge))
                    self.assertFalse(bridge.difficulty_info(value)['ok'])

    def test_malformed_choice_and_removal_indices_leave_state_rng_and_file_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            for action, key in (('choose', 'number'), ('remove', 'index')):
                with self.subTest(action=action):
                    bridge = DesktopBridge(Path(tmp) / 'current.json')
                    bridge.new_game(42, 7)
                    if action == 'choose':
                        bridge._engine.s.pending = [PendingChoice('ingredient', ['water'])]
                    else:
                        bridge._engine.s.tokens['remove'] = 1
                    bridge.get_state()  # Record the published fixture before the comparison.
                    save_game(bridge._engine.s, bridge._save_path)
                    before = self.snapshot(bridge)
                    for value in self.invalid:
                        with self.subTest(value=value):
                            self.assertFalse(bridge.action(action, {key: value})['ok'])
                            self.assertEqual(before, self.snapshot(bridge))

    def test_decimal_text_and_integer_actions_still_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            for index in (1, '1', ' +1 '):
                bridge = DesktopBridge(Path(tmp) / 'current.json')
                self.assertTrue(bridge.new_game('18446744073709551615', '15')['ok'])
                self.assertEqual(15, bridge.difficulty_info('15')['difficulty'])
                bridge._engine.s.pending = [PendingChoice('ingredient', ['water'])]
                self.assertTrue(bridge.action('choose', {'number': index})['ok'])
                bridge._engine.s.tokens['remove'] = 1
                self.assertTrue(bridge.action('remove', {'index': index})['ok'])

    def test_seed_text_validation_matches_form_without_python_only_separators(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = DesktopBridge(Path(tmp) / 'current.json')
            for seed in ('1_000', '1e3', '0x2a', '', '１２'):
                with self.subTest(seed=seed):
                    bridge.new_game(42)
                    before = self.snapshot(bridge)
                    self.assertFalse(bridge.new_game(seed)['ok'])
                    self.assertEqual(before, self.snapshot(bridge))


if __name__ == '__main__':
    unittest.main()
