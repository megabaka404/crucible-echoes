from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from crucible_echoes.engine import GameEngine
from crucible_echoes.save import save_game
from desktop.bridge import DesktopBridge
from desktop.view_model import build_view_state


class DesktopSnapshotTransactionTests(unittest.TestCase):
    def test_automatic_startup_load_rejects_unrenderable_save_without_committing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.json"
            bad = GameEngine()
            bad.new_game(44)
            bad.s.ingredients[0].def_id = "missing_definition"
            save_game(bad.s, path)
            before = path.read_bytes()
            bridge = DesktopBridge(path)
            result = bridge.get_state()
            self.assertFalse(result["ok"])
            self.assertEqual("menu", result["screen"])
            self.assertIsNone(bridge._engine)
            self.assertEqual(before, path.read_bytes())
            self.assertFalse(bridge.get_state()["ok"])
            self.assertIsNone(bridge._engine)
            self.assertTrue(bridge.new_game(45)["ok"])
            self.assertEqual(45, bridge.get_state()["seed"])

    def test_automatic_load_validates_view_before_commit_and_can_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.json"
            core = GameEngine()
            core.new_game(44)
            save_game(core.s, path)
            bridge = DesktopBridge(path)
            before = path.read_bytes()
            with patch("desktop.bridge.build_view_state", side_effect=IndexError("invalid board view")):
                result = bridge.get_state()
            self.assertFalse(result["ok"])
            self.assertEqual("menu", result["screen"])
            self.assertIsNone(bridge._engine)
            self.assertEqual(before, path.read_bytes())
            self.assertTrue(bridge.get_state()["ok"])
            self.assertEqual(core.s.to_dict(), bridge._engine.s.to_dict())

    def test_explicit_save_validates_view_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.json"
            bridge = DesktopBridge(path)
            bridge.new_game(44)
            # Represent a valid runtime change not yet saved to disk.
            bridge._engine.s.gold += 1
            before = deepcopy(bridge._engine.s.to_dict()), path.read_bytes()
            def reject_save(engine, payload):
                if payload.get("action") == "save":
                    raise TypeError("save view rejected")
                return build_view_state(engine, payload)
            with patch("desktop.bridge.build_view_state", reject_save):
                result = bridge.save()
            self.assertFalse(result["ok"])
            self.assertEqual(before, (bridge._engine.s.to_dict(), path.read_bytes()))
            self.assertTrue(bridge.save()["ok"])

    def test_failed_recovery_view_returns_menu_without_losing_active_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.json"
            bridge = DesktopBridge(path)
            bridge.new_game(44)
            original = bridge._engine
            before = deepcopy(original.s.to_dict()), path.read_bytes(), original.r.state
            with patch("desktop.bridge.build_view_state", side_effect=TypeError("renderer unavailable")):
                for operation in (bridge.get_state, bridge.save):
                    result = operation()
                    self.assertFalse(result["ok"])
                    self.assertEqual("menu", result["screen"])
                    self.assertIs(original, bridge._engine)
                    self.assertEqual(before, (original.s.to_dict(), path.read_bytes(), original.r.state))
            self.assertTrue(bridge.get_state()["ok"])

    def test_unknown_definition_save_does_not_replace_active_game_or_menu(self):
        for active in (True, False):
            with self.subTest(active=active), tempfile.TemporaryDirectory() as directory:
                current, broken = Path(directory) / "current.json", Path(directory) / "broken.json"
                bridge = DesktopBridge(current)
                if active:
                    self.assertTrue(bridge.new_game(42)["ok"])
                original, original_path = bridge._engine, bridge._save_path
                before = deepcopy(original.s.to_dict()) if original else None
                bad = GameEngine()
                bad.new_game(43)
                bad.s.ingredients[0].def_id = "missing_definition"
                save_game(bad.s, broken)
                bytes_before = broken.read_bytes()
                result = bridge.continue_game(str(broken))
                self.assertFalse(result["ok"])
                self.assertIs(original, bridge._engine)
                self.assertEqual(original_path, bridge._save_path)
                self.assertEqual(bytes_before, broken.read_bytes())
                if original:
                    self.assertEqual(before, original.s.to_dict())
                self.assertTrue(bridge.get_state()["ok"])

    def test_action_view_failure_keeps_state_rng_and_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.json"
            bridge = DesktopBridge(path)
            bridge.new_game(42)
            original = bridge._engine
            before = deepcopy(original.s.to_dict()), original.r.state, path.read_bytes()
            def reject_candidate(engine, payload):
                if engine.s.spin:
                    raise TypeError("cannot render candidate")
                return build_view_state(engine, payload)
            with patch("desktop.bridge.build_view_state", reject_candidate):
                result = bridge.action("end_turn")
            self.assertFalse(result["ok"])
            self.assertIs(original, bridge._engine)
            self.assertEqual(before, (original.s.to_dict(), original.r.state, path.read_bytes()))
            self.assertTrue(bridge.action("end_turn")["ok"])

    def test_new_game_view_failure_preserves_existing_target_and_previous_path(self):
        with tempfile.TemporaryDirectory() as directory:
            current, target = Path(directory) / "current.json", Path(directory) / "other.json"
            bridge = DesktopBridge(current)
            bridge.new_game(42)
            other = GameEngine()
            other.new_game(41)
            save_game(other.s, target)
            original, before_target = bridge._engine, target.read_bytes()
            before = deepcopy(original.s.to_dict())
            def reject_candidate(engine, payload):
                if engine.s.seed == 43:
                    raise ValueError("candidate view rejected")
                return build_view_state(engine, payload)
            with patch("desktop.bridge.build_view_state", reject_candidate):
                result = bridge.new_game(43, save_path=str(target))
            self.assertFalse(result["ok"])
            self.assertIs(original, bridge._engine)
            self.assertEqual(before, original.s.to_dict())
            self.assertEqual(current.resolve(), bridge._save_path)
            self.assertEqual(before_target, target.read_bytes())
