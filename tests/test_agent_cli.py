from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from crucible_echoes.cli import main
from crucible_echoes.engine import GameEngine
from crucible_echoes.model import PendingChoice
from crucible_echoes.save import load_game, save_game


class AgentCliTests(unittest.TestCase):
    def test_terminal_and_pending_item_actions_return_errors_without_saving(self):
        for status, pending in (("lost", False), ("won", False), ("playing", True)):
            for action, item_id in (("use", "sandpaper_box"), ("toggle", "ban")):
                with self.subTest(status=status, pending=pending, action=action), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "state.json"
                    engine = GameEngine()
                    engine.new_game(42)
                    engine.s.items.extend(["sandpaper_box", "ban"])
                    engine.s.status = status
                    if pending:
                        engine.s.pending = [PendingChoice("ingredient", ["water"])]
                    save_game(engine.s, path)
                    before = path.read_bytes()
                    code, payload = self.call_agent(path, action, item_id)
                    self.assertEqual(2, code)
                    self.assertFalse(payload["ok"])
                    self.assertNotIn(f"{action} {item_id}", payload["available_actions"])
                    self.assertEqual(before, path.read_bytes())

    def call_agent(self, save: Path, *args: str) -> tuple[int, dict]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["agent", *args, "--save", str(save)])
        lines = [line for line in output.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 1, output.getvalue())
        self.assertTrue(lines[0].startswith("[STATE] "), lines[0])
        return code, json.loads(lines[0][len("[STATE] "):])

    def test_agent_is_one_step_and_persists_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            save = Path(directory) / "agent.json"
            code, state = self.call_agent(save, "new", "--seed", "42", "--difficulty", "1")
            self.assertEqual(code, 0)
            self.assertEqual(state["protocol"], "crucible-echoes-agent/v1")
            self.assertTrue(state["ok"])
            self.assertEqual(state["action"], "new")
            self.assertIn("spin", state["available_actions"])
            self.assertIn("state", state)
            self.assertEqual(len(state["ingredients"]), 5)
            self.assertEqual(state["pending_choices"], [])

            code, state = self.call_agent(save, "spin")
            self.assertEqual(code, 0)
            self.assertEqual(state["spin"], 1)
            self.assertEqual(state["action"], "spin")
            self.assertEqual(len(state["pending_choices"]), 1)
            self.assertEqual(state["available_actions"][:2], ["status", "inventory"])
            self.assertTrue(any(action.startswith("choose ") for action in state["available_actions"]))
            self.assertEqual(load_game(save).spin, 1)

            code, state = self.call_agent(save, "choose", "1")
            self.assertEqual(code, 0)
            self.assertEqual(state["action"], "choose")
            self.assertEqual(state["pending_choices"], [])
            self.assertEqual(load_game(save).spin, 1)

    def test_invalid_agent_action_still_returns_state_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            save = Path(directory) / "agent.json"
            self.call_agent(save, "new", "--seed", "9")
            self.call_agent(save, "spin")
            code, state = self.call_agent(save, "choose", "999")
            self.assertEqual(code, 2)
            self.assertFalse(state["ok"])
            self.assertEqual(state["error"]["type"], "GameError")
            self.assertEqual(state["state"]["spin"], 1)

    def test_agent_save_resume_keeps_rng_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            save = Path(directory) / "agent.json"
            self.call_agent(save, "new", "--seed", "314159")
            self.call_agent(save, "spin")
            self.call_agent(save, "skip")
            self.call_agent(save, "spin")

            direct = GameEngine()
            direct.new_game(314159)
            direct.spin()
            direct.skip()
            direct.spin()
            resumed = load_game(save)
            self.assertEqual(resumed.last_board, direct.s.last_board)
            self.assertEqual(resumed.pending[0].offers, direct.s.pending[0].offers)
            self.assertEqual(resumed.rng_state, direct.s.rng_state)

    def test_agent_can_optionally_exchange_a_consumable_box(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            save = Path(directory) / "agent.json"
            engine = GameEngine()
            engine.new_game(2718)
            engine.add_item("sandpaper_box")
            save_game(engine.s, save)

            code, before = self.call_agent(save, "status")
            self.assertEqual(0, code)
            self.assertIn("use sandpaper_box", before["available_actions"])

            code, after = self.call_agent(save, "use", "sandpaper_box")
            self.assertEqual(0, code)
            self.assertNotIn("sandpaper_box", after["items"])
            self.assertEqual(2, sum(row["id"] == "sandpaper" for row in after["ingredients"]))
            self.assertNotIn("use sandpaper_box", after["available_actions"])


if __name__ == "__main__":
    unittest.main()
