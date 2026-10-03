from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from crucible_echoes.engine import GameEngine
from crucible_echoes.save import load_game, save_game


class SaveAtomicityTests(unittest.TestCase):
    def state(self, seed):
        engine = GameEngine()
        engine.new_game(seed)
        return engine.s

    def test_overlapping_writers_use_distinct_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "current.json"
            states = [self.state(seed) for seed in (41, 42)]
            barrier = Barrier(2)
            names = []
            replace = Path.replace
            fsync = os.fsync
            def simultaneous_flush(descriptor):
                fsync(descriptor)
                barrier.wait(timeout=10)
            def record_replace(source, destination):
                names.append(str(source))
                return replace(source, destination)
            with patch("crucible_echoes.save.os.fsync", simultaneous_flush), patch.object(Path, "replace", record_replace), ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda state: save_game(state, target), states))
            self.assertEqual([target, target], results)
            self.assertEqual(2, len(set(names)))
            self.assertIn(load_game(target).to_dict(), [state.to_dict() for state in states])
            self.assertEqual([target], list(Path(directory).iterdir()))

    def test_eight_writers_can_commit_one_hundred_complete_states(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "current.json"
            states = [self.state(seed) for seed in range(8)]
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda index: save_game(states[index % 8], target), range(100)))
            self.assertEqual([target] * 100, results)
            self.assertIn(load_game(target).to_dict(), [state.to_dict() for state in states])
            self.assertEqual([target], list(Path(directory).iterdir()))

    def test_failed_replace_keeps_old_save_and_removes_only_own_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "current.json"
            save_game(self.state(43), target)
            sentinel = target.with_suffix(".json.tmp")
            sentinel.write_text("unrelated existing temporary file", encoding="utf-8")
            before = target.read_bytes()
            with patch.object(Path, "replace", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    save_game(self.state(44), target)
            self.assertEqual(before, target.read_bytes())
            self.assertEqual("unrelated existing temporary file", sentinel.read_text(encoding="utf-8"))
            self.assertEqual({target, sentinel}, set(Path(directory).iterdir()))

    def test_serialization_failure_preserves_disk_and_creates_no_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "current.json"
            state = self.state(45)
            save_game(state, target)
            before = target.read_bytes()
            state.flags["invalid"] = object()
            with self.assertRaises(TypeError):
                save_game(state, target)
            self.assertEqual(before, target.read_bytes())
            self.assertEqual([target], list(Path(directory).iterdir()))

    def test_format_state_and_rng_are_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "current.json"
            state = self.state(46)
            before = state.to_dict()
            self.assertEqual(target, save_game(state, target))
            self.assertEqual(json.dumps(before, ensure_ascii=False, indent=2), target.read_text(encoding="utf-8"))
            self.assertEqual(before, state.to_dict())
            self.assertEqual(before, load_game(target).to_dict())

    def test_transient_permission_failure_retries_but_persistent_failure_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "current.json"
            state = self.state(47)
            replace = Path.replace
            attempts = []
            def transient(source, destination):
                attempts.append(str(source))
                if len(attempts) == 1:
                    raise PermissionError("temporarily locked")
                return replace(source, destination)
            with patch.object(Path, "replace", transient), patch("crucible_echoes.save.time.sleep") as sleep:
                save_game(state, target)
            self.assertEqual(2, len(attempts))
            self.assertEqual(1, len(set(attempts)))
            sleep.assert_called_once_with(0.01)
            before = target.read_bytes()
            with patch.object(Path, "replace", side_effect=PermissionError("read-only")) as operation, patch("crucible_echoes.save.time.sleep") as sleep:
                with self.assertRaisesRegex(PermissionError, "read-only"):
                    save_game(self.state(48), target)
            self.assertEqual(5, operation.call_count)
            self.assertEqual(4, sleep.call_count)
            self.assertEqual(before, target.read_bytes())
            self.assertEqual([target], list(Path(directory).iterdir()))
