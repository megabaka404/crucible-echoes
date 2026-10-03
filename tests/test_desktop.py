from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import inspect
import importlib
import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crucible_echoes.engine import GameEngine, GameError
from crucible_echoes.save import save_game
from crucible_echoes.model import GameState, PendingChoice
from desktop.bridge import DesktopBridge, default_save_path
from desktop.view_model import build_view_state

NODE_EXECUTABLE = os.environ.get("CRUCIBLE_NODE_EXECUTABLE") or shutil.which("node")


class DesktopAdapterTests(unittest.TestCase):
    def test_large_decimal_seed_roundtrips_without_javascript_numeric_precision(self):
        with tempfile.TemporaryDirectory() as directory:
            for seed in ("18446744073709551615", "9007199254740993", "-9007199254740993", "0"):
                bridge = DesktopBridge(Path(directory) / "current.json")
                view = bridge.new_game(seed)
                core = GameEngine()
                core.new_game(int(seed))
                self.assertTrue(view["ok"])
                self.assertEqual(seed, view["seed_text"])
                self.assertEqual(core.s.to_dict(), bridge._engine.s.to_dict())
                self.assertEqual(core.r.state, bridge._engine.r.state)

    def test_noninteger_seed_rejection_keeps_previous_game_and_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.json"
            bridge = DesktopBridge(path)
            bridge.new_game(7)
            before = copy.deepcopy(bridge._engine.s.to_dict()), path.read_bytes()
            for seed in (True, 1.5, float("inf"), None):
                with self.subTest(seed=seed):
                    bridge.new_game(7)
                    self.assertFalse(bridge.new_game(seed)["ok"])
                    self.assertEqual(before, (bridge._engine.s.to_dict(), path.read_bytes()))

    def test_failed_action_save_keeps_state_rng_and_disk_then_retry_matches_core(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.json"
            bridge = DesktopBridge(path)
            bridge.new_game(42)
            original = bridge._engine
            before = copy.deepcopy(original.s.to_dict()), path.read_bytes(), original.r.state
            expected = GameEngine(original.catalog).bind(GameState.from_dict(copy.deepcopy(before[0])))
            expected.spin()
            with patch("desktop.bridge.save_game", side_effect=OSError("permission denied")):
                failed = bridge.action("end_turn")
            self.assertFalse(failed["ok"])
            self.assertIn("本次操作未生效", failed["error"]["message"])
            self.assertIs(original, bridge._engine)
            self.assertEqual(before, (bridge._engine.s.to_dict(), path.read_bytes(), bridge._engine.r.state))
            self.assertTrue(bridge.action("end_turn")["ok"])
            self.assertEqual(expected.s.to_dict(), bridge._engine.s.to_dict())
            self.assertEqual(expected.r.state, bridge._engine.r.state)
            self.assertIs(original.catalog, bridge._engine.catalog)

    def test_every_mutating_action_is_not_committed_when_autosave_fails(self):
        cases = (("choose", {"number": 1}), ("skip", None), ("reroll", None),
                 ("remove", {"index": 1}), ("use", {"item_id": "sandpaper_box"}), ("toggle", {"item_id": "ban"}))
        for action, payload in cases:
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "current.json"
                bridge = DesktopBridge(path)
                bridge.new_game(43)
                bridge._engine.s.items.extend(["sandpaper_box", "ban"])
                bridge._engine.s.tokens.update(remove=1, roll=1)
                if action in {"choose", "skip", "reroll"}:
                    bridge._engine.s.pending = [PendingChoice("ingredient", ["water", "paper"])]
                save_game(bridge._engine.s, path)
                before = copy.deepcopy(bridge._engine.s.to_dict()), path.read_bytes(), bridge._engine.r.state
                with patch("desktop.bridge.save_game", side_effect=OSError("disk full")):
                    result = bridge.action(action, payload)
                self.assertFalse(result["ok"])
                self.assertIn("存档写入失败", result["error"]["message"])
                self.assertEqual(before, (bridge._engine.s.to_dict(), path.read_bytes(), bridge._engine.r.state))

    def test_explicit_quit_requires_successful_save_but_menu_can_quit(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = DesktopBridge(Path(directory) / "current.json")
            closed = []
            bridge._window = SimpleNamespace(destroy=lambda: closed.append(True))
            self.assertTrue(bridge.close()["ok"])
            self.assertEqual(1, len(closed))
            bridge.new_game(44)
            with patch("desktop.bridge.save_game", side_effect=OSError("disk full")):
                self.assertFalse(bridge.close()["ok"])
            self.assertEqual(1, len(closed))
            self.assertTrue(bridge.close()["ok"])
            self.assertEqual(2, len(closed))

    def test_partial_core_failure_does_not_commit_runtime_or_rng(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = DesktopBridge(Path(directory) / "current.json")
            bridge.new_game(45)
            before = copy.deepcopy(bridge._engine.s.to_dict()), bridge._engine.r.state
            def broken_spin(engine):
                engine.s.gold += 999
                engine.r.next_u64()
                engine._round_events["broken"] = 1
                raise GameError("test settlement failed")
            with patch.object(GameEngine, "spin", broken_spin):
                self.assertFalse(bridge.action("end_turn")["ok"])
            self.assertEqual(before, (bridge._engine.s.to_dict(), bridge._engine.r.state))
            self.assertNotIn("broken", bridge._engine._round_events)

    def test_transactional_bridge_matches_core_trajectories_in_all_modes(self):
        for mode in ("none", "giant", "rapid", "blind_box", "minimal", "mutation"):
            for difficulty in (7, 15):
                with self.subTest(mode=mode, difficulty=difficulty), tempfile.TemporaryDirectory() as directory:
                    bridge = DesktopBridge(Path(directory) / "current.json")
                    bridge.new_game(20261014, difficulty, mode)
                    core = GameEngine()
                    core.new_game(20261014, difficulty, mode)
                    for step in range(80):
                        if core.s.status != "playing" or (core.s.spin >= 12 and not core.s.pending):
                            break
                        if core.s.pending:
                            core.choose(1)
                            result = bridge.action("choose", {"number": 1})
                        else:
                            core.spin()
                            result = bridge.action("end_turn")
                        self.assertTrue(result["ok"], result.get("error"))
                        self.assertEqual(core.s.to_dict(), bridge._engine.s.to_dict(), (mode, difficulty, step))
                        self.assertEqual(core.r.state, bridge._engine.r.state)
                        self.assertEqual(core._all_adjacent, bridge._engine._all_adjacent)
                        self.assertEqual(core._panorama, bridge._engine._panorama)
                        for instance in bridge._engine._board:
                            live = next((x for x in bridge._engine.s.ingredients if x.uid == instance.uid), None)
                            if live is not None:
                                self.assertIs(instance, live)

    def test_bridge_rejects_terminal_and_pending_active_operations_without_saving(self):
        for status, pending in (("lost", False), ("won", False), ("playing", True)):
            for action, item in (("use", "sandpaper_box"), ("toggle", "ban")):
                with self.subTest(status=status, action=action), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "state.json"
                    bridge = DesktopBridge(path)
                    bridge.new_game(seed=42, difficulty=7)
                    bridge._engine.s.items.extend(["sandpaper_box", "ban"])
                    bridge._engine.s.status = status
                    if pending:
                        bridge._engine.s.pending = [PendingChoice("ingredient", ["water"])]
                    save_game(bridge._engine.s, path)
                    before = copy.deepcopy(bridge._engine.s.to_dict()), path.read_bytes(), bridge._engine.r.state
                    result = bridge.action(action, {"item_id": item})
                    self.assertFalse(result["ok"])
                    self.assertEqual(before, (bridge._engine.s.to_dict(), path.read_bytes(), bridge._engine.r.state))

    def test_view_state_is_complete_and_uses_core_board(self) -> None:
        engine = GameEngine()
        engine.new_game(42, difficulty=7, fun_mode="minimal")
        view = build_view_state(engine)
        self.assertEqual(view["protocol"], "crucible-echoes-desktop/v1")
        self.assertEqual(view["difficulty"], 7)
        self.assertEqual(view["fun_mode"], "minimal")
        self.assertEqual(view["board_capacity"], 12)
        self.assertEqual(view["ui"]["capacity"], 12)
        self.assertIn("spin", view["available_actions"])
        self.assertEqual(len(view["board"]), len(view["ingredients"]))

    def test_initial_pool_is_visible_before_first_spin(self) -> None:
        engine = GameEngine()
        engine.new_game(42)
        view = build_view_state(engine)
        self.assertEqual(len(view["board"]), 5)
        self.assertEqual(view["board"][0]["id"], engine.s.ingredients[0].def_id)
        self.assertEqual(view["board"][0]["coord"], [0, 0])
        self.assertIn("rarity", view["board"][0])
        self.assertIn("base", view["board"][0])
        self.assertIn("spin", view["available_actions"])

    def test_geometry_uses_full_layout_even_with_small_pool(self) -> None:
        for mode, rows, columns, capacity in (("none", 4, 5, 20), ("minimal", 3, 4, 12), ("giant", 5, 8, 40)):
            with self.subTest(mode=mode):
                engine = GameEngine()
                engine.new_game(42, fun_mode=mode)
                view = build_view_state(engine)
                self.assertEqual((view["board_rows"], view["board_columns"], view["board_capacity"]), (rows, columns, capacity))
                self.assertEqual(view["board_row_offset"], 1)
                engine.s.expanded = True
                expanded = build_view_state(engine)
                self.assertEqual(expanded["board_columns"], columns)
                self.assertEqual(expanded["board_capacity"], capacity + 1)
                self.assertEqual(expanded["board_row_offset"], 2)

    def test_stable_values_share_core_minimal_and_global_value_layers(self) -> None:
        engine = GameEngine()
        engine.new_game(42, fun_mode="minimal")
        engine.s.items.append("animal_feed")
        engine.s.flags["global_permanent_bonuses"]["kitten"] = 2
        before = copy.deepcopy(engine.s.to_dict())
        view = build_view_state(engine)
        kitten = next(row for row in view["ingredients"] if row["id"] == "kitten")
        self.assertEqual(kitten["stable_value"], 7)  # base1 + global2x2 + item1 + minimal1
        board_kitten = next(row for row in view["board"] if row["id"] == "kitten")
        self.assertEqual(board_kitten["value"], kitten["stable_value"])
        self.assertEqual(board_kitten["value_kind"], "stable")
        self.assertEqual(before, engine.s.to_dict())

    def test_settled_values_are_not_replaced_by_stable_values(self) -> None:
        engine = GameEngine()
        engine.new_game(42)
        engine.spin()
        view = build_view_state(engine)
        self.assertEqual([row["value"] for row in view["board"]], [row["value"] for row in engine.s.last_board])
        self.assertTrue(all(row["value_kind"] == "settled" for row in view["board"]))

    def test_peace_progress_displays_victory_goal_not_zero_order_cost(self) -> None:
        engine = GameEngine()
        engine.new_game(42)
        engine.s.peace_mode = True
        engine.s.peace_order = 1
        engine.s.gold = 250_000
        engine.s.spins_left = 7
        view = build_view_state(engine)
        self.assertEqual(view["order_amount"], 0)
        self.assertEqual(view["peace_target"], 1_000_000)
        self.assertEqual(view["order_progress"], 0.25)

    def test_settled_adjacency_keeps_decayed_one_spin_topology(self) -> None:
        engine = GameEngine()
        engine.new_game(42)
        engine.s.ingredients.clear()
        for _ in range(20):
            engine.add_ingredient("water")
        engine.s.flags["all_adjacent_spins"] = 1
        engine.spin()
        self.assertFalse(engine.s.flags.get("all_adjacent_spins", 0))
        self.assertTrue(all(len(row["neighbors"]) == 19 for row in build_view_state(engine)["board"]))
        resumed = GameEngine().bind(copy.deepcopy(engine.s))
        self.assertTrue(all(len(row["neighbors"]) == 19 for row in build_view_state(resumed)["board"]))

    def test_panorama_and_departed_neighbors_use_core_topology(self) -> None:
        engine = GameEngine()
        engine.new_game(42)
        engine.s.ingredients.clear()
        for _ in range(20):
            engine.add_ingredient("water")
        engine.s.items.append("panorama_mirror")
        engine.s.spin = 2
        engine.spin()
        view = build_view_state(engine)
        self.assertEqual(len(view["board"][0]["neighbors"]), 19)
        self.assertIn(19, view["board"][6]["neighbors"])
        engine.skip()
        departed_uid = view["board"][0]["uid"]
        slot = next(index for index, instance in enumerate(engine.s.ingredients, 1) if instance.uid == departed_uid)
        engine.s.tokens["remove"] = 1
        engine.remove(slot)
        resumed = GameEngine().bind(copy.deepcopy(engine.s))
        updated = build_view_state(resumed)
        self.assertFalse(updated["board"][0]["present"])
        self.assertNotIn(0, updated["board"][6]["neighbors"])

    def test_smoke_completion_is_isolated_and_late_success_cannot_override_timeout(self) -> None:
        with patch.dict(sys.modules, {"webview": SimpleNamespace(renderer="mock")}):
            smoke = importlib.import_module("desktop.smoke")
            with tempfile.TemporaryDirectory() as directory, patch.object(smoke.threading, "Timer"):
                bridge = smoke.SmokeBridge(Path(directory) / "smoke.json")
                bridge.new_game(9001)
                before = copy.deepcopy(bridge._engine.s.to_dict())
                saved = bridge._save_path.read_bytes()
                bridge._timeout()
                self.assertFalse(bridge._result["ok"])
                late = bridge.smoke_result(json.dumps({"ok": True}))
                self.assertFalse(late["ok"])
                self.assertFalse(bridge._result["ok"])
                self.assertEqual(bridge._engine.s.to_dict(), before)
                self.assertEqual(bridge._save_path.read_bytes(), saved)

    def test_remove_specs_cover_pool_ingredients_not_on_current_board(self) -> None:
        engine = GameEngine()
        engine.new_game(42)
        for _ in range(20):
            engine.add_ingredient("paper")
        engine.spin()
        engine.skip()
        engine.s.tokens["remove"] = 1
        view = build_view_state(engine)
        self.assertEqual(len(view["board"]), 20)
        self.assertIn({"action": "remove", "index": 25, "id": "paper"}, view["available_action_specs"])

    def test_bridge_new_spin_choose_and_save_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            save = Path(directory) / "current.json"
            bridge = DesktopBridge(save)
            state = bridge.new_game(seed=42, difficulty=1)
            self.assertTrue(state["ok"])
            self.assertTrue(save.exists())
            state = bridge.action("end_turn")
            self.assertEqual(state["spin"], 1)
            self.assertEqual(len(state["pending_choices"]), 1)
            state = bridge.action("choose", {"number": 1})
            self.assertEqual(state["pending_choices"], [])
            resumed = DesktopBridge(save).continue_game()
            self.assertEqual(resumed["spin"], 1)
            self.assertEqual(resumed["state"] if "state" in resumed else resumed["gold"], state["gold"])

    def test_serialized_methods_keep_pywebview_named_parameters(self) -> None:
        self.assertEqual(inspect.getfullargspec(DesktopBridge.new_game).args, ["self", "seed", "difficulty", "fun_mode", "save_path"])
        self.assertEqual(inspect.getfullargspec(DesktopBridge.action).args, ["self", "action", "payload"])
        self.assertEqual(inspect.getfullargspec(DesktopBridge.continue_game).args, ["self", "save_path"])

    def test_pending_offers_expose_rarity_and_base_for_choice_cards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = DesktopBridge(Path(directory) / "current.json")
            bridge.new_game(seed=42)
            state = bridge.action("end_turn")
            offers = state["pending_choices"][0]["offers"]
            self.assertTrue(offers)
            definition = offers[0]["definition"]
            self.assertIn("rarity", definition)
            self.assertIn("base", definition)

    def test_bridge_routes_reroll_and_delete_dispatch(self) -> None:
        """The desktop action adapter must reach the existing token actions."""

        with tempfile.TemporaryDirectory() as directory:
            bridge = DesktopBridge(Path(directory) / "current.json")
            bridge.new_game(seed=42)
            bridge._engine.s.tokens["roll"] = 1
            state = bridge.action("end_turn")
            self.assertEqual(len(state["pending_choices"]), 1)
            rerolled = bridge.action("reroll")
            self.assertTrue(rerolled["ok"])
            self.assertEqual(rerolled["tokens"]["roll"], 0)
            bridge.action("choose", {"number": 1})

            bridge._engine.s.tokens["remove"] = 1
            before = len(bridge._engine.s.ingredients)
            removed = bridge.action("remove", {"index": 1})
            self.assertTrue(removed["ok"])
            self.assertEqual(len(bridge._engine.s.ingredients), before - 1)
            self.assertEqual(removed["tokens"]["remove"], 0)

    def test_same_seed_core_and_desktop_actions_keep_rng_sequence(self) -> None:
        direct = GameEngine()
        direct.new_game(2718, difficulty=6, fun_mode="minimal")
        with tempfile.TemporaryDirectory() as directory:
            bridge = DesktopBridge(Path(directory) / "current.json")
            bridge.new_game(seed=2718, difficulty=6, fun_mode="minimal")
            direct.spin()
            desktop_state = bridge.action("end_turn")
            self.assertEqual(bridge._engine.s.last_board, direct.s.last_board)
            self.assertEqual(
                [row["id"] for row in desktop_state["pending_choices"][0]["offers"]],
                direct.s.pending[0].offers,
            )
            direct.choose(1)
            bridge.action("choose", {"number": 1})
            self.assertEqual(bridge._engine.s.rng_state, direct.s.rng_state)

    def test_preview_does_not_mutate_rng_or_game_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = DesktopBridge(Path(directory) / "current.json")
            bridge.new_game(seed=314159)
            before = copy.deepcopy(bridge._engine.s.to_dict())
            result = bridge.preview()
            after = bridge._engine.s.to_dict()
            self.assertTrue(result["ok"])
            self.assertEqual(before, after)

    def test_legacy_save_and_modes_are_visible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.json"
            engine = GameEngine()
            engine.new_game(9, difficulty=15, fun_mode="mutation")
            raw = engine.s.to_dict()
            raw.pop("fun_mode", None)
            # A legacy save without newer mode fields must still load through
            # the same bridge path and receive safe defaults.
            raw.pop("peace_mode", None)
            raw.pop("endless_mode", None)
            raw.pop("endless_order", None)
            raw.pop("endless_target", None)
            from crucible_echoes.model import GameState
            save_game(GameState.from_dict(raw), path)
            state = DesktopBridge(path).continue_game()
            self.assertEqual(state["fun_mode"], "none")
            self.assertEqual(state["difficulty"], 15)

    def test_difficulty_info_is_cumulative_and_data_driven(self) -> None:
        info = DesktopBridge().difficulty_info(15)
        self.assertTrue(info["ok"])
        thresholds = [row["difficulty"] for row in info["rules"]]
        self.assertEqual(thresholds, list(range(2, 16)))
        self.assertEqual(info["rules"][-1]["rule"]["post_order_gold_deduction"]["amount"], 5)

    def test_default_save_path_is_user_local_and_legacy_save_is_copied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_root = root / "old_install"
            legacy_path = legacy_root / ".saves" / "current.json"
            engine = GameEngine()
            engine.new_game(123, difficulty=7)
            save_game(engine.s, legacy_path)
            legacy_bytes = legacy_path.read_bytes()
            with patch.dict("os.environ", {"LOCALAPPDATA": str(root / "user_data")}):
                with patch("desktop.bridge.Path.cwd", return_value=legacy_root):
                    bridge = DesktopBridge()
                self.assertEqual(bridge._save_path, default_save_path())
                self.assertTrue(bridge._save_path.exists())
                self.assertEqual(bridge.get_state()["seed"], 123)
            self.assertEqual(legacy_path.read_bytes(), legacy_bytes)

    def test_explicit_save_path_stays_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            explicit = Path(directory) / "chosen.json"
            bridge = DesktopBridge(explicit)
            self.assertEqual(bridge._save_path, explicit.resolve())
            self.assertTrue(bridge.new_game(8)["ok"])
            self.assertTrue(explicit.exists())

    def test_failed_new_game_does_not_replace_running_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = DesktopBridge(Path(directory) / "current.json")
            bridge.new_game(42)
            before = copy.deepcopy(bridge._engine.s.to_dict())
            save_path = bridge._save_path
            failed = bridge.new_game(99, difficulty=99, save_path=str(Path(directory) / "other.json"))
            self.assertFalse(failed["ok"])
            self.assertEqual(bridge._engine.s.to_dict(), before)
            self.assertEqual(bridge._save_path, save_path)

    def test_failed_continue_keeps_running_state_and_target_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = DesktopBridge(Path(directory) / "current.json")
            bridge.new_game(42)
            before = copy.deepcopy(bridge._engine.s.to_dict())
            failed = bridge.continue_game(str(Path(directory) / "missing.json"))
            self.assertFalse(failed["ok"])
            self.assertEqual(bridge._engine.s.to_dict(), before)
            self.assertEqual(bridge._save_path.name, "current.json")

    def test_concurrent_calls_serialize_state_and_atomic_saves(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = DesktopBridge(Path(directory) / "current.json")
            bridge.new_game(42)
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(lambda _: bridge.save(), range(20)))
            self.assertTrue(all(result["ok"] for result in results))
            with ThreadPoolExecutor(max_workers=2) as executor:
                actions = list(executor.map(lambda _: bridge.action("end_turn"), range(2)))
            self.assertEqual(sum(bool(result["ok"]) for result in actions), 1)
            self.assertEqual(bridge._engine.s.spin, 1)
            self.assertEqual(DesktopBridge(bridge._save_path).continue_game()["spin"], 1)

    def test_damaged_save_reports_error_but_keeps_menu_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text("not json", encoding="utf-8")
            state = DesktopBridge(path).get_state()
            self.assertFalse(state["ok"])
            self.assertEqual(state["screen"], "menu")
            self.assertIn("new_game", state["available_actions"])

    @unittest.skipUnless(NODE_EXECUTABLE, "Node is optional; set CRUCIBLE_NODE_EXECUTABLE to run frontend regression")
    def test_frontend_first_hints_and_unavailable_browser_storage(self) -> None:
        result = subprocess.run(
            [str(NODE_EXECUTABLE), "tests/frontend_hints.js"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("frontend hints passed", result.stdout)

    @unittest.skipUnless(NODE_EXECUTABLE, "Node is optional; set CRUCIBLE_NODE_EXECUTABLE to run frontend regression")
    def test_frontend_render_navigation_and_inflight_regressions(self) -> None:
        # A lightweight DOM stub executes the shipped JS; this is a frontend
        # regression check, not a substitute for a native WebView smoke test.
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const nodes = [], ids = new Map(), hooks = {};
class El {
  constructor() { this.children=[]; this.dataset={}; this.classes=new Set(); this.disabled=false; this.value='1'; this.style={setProperty(){}}; nodes.push(this); this.classList={add:x=>this.classes.add(x),remove:x=>this.classes.delete(x),toggle:(x,v)=>v===undefined?(this.classes.has(x)?this.classes.delete(x):this.classes.add(x)):(v?this.classes.add(x):this.classes.delete(x))}; }
  set className(v) { this.classes=new Set(v.split(/\s+/)); }
  set innerHTML(v) { this.html=v; this.children=[]; }
  appendChild(x) { this.children.push(x); }
  addEventListener(type, handler) { if(!this.listeners) this.listeners={}; this.listeners[type]=handler; }
  setAttribute(key, value) { this[key]=value; }
  scrollIntoView() {}
}
const match=(e,s)=>s.split('.').filter(Boolean).every(c=>e.classes.has(c));
global.document={body:new El(),getElementById(id){if(!ids.has(id))ids.set(id,new El());return ids.get(id);},createElement(){return new El();},querySelectorAll(s){return s[0]==='.'?nodes.filter(e=>s.split(',').some(c=>match(e,c.trim()))):[];},querySelector(s){return this.querySelectorAll(s)[0]||null;},addEventListener(t,f){hooks[t]=f;}};
global.localStorage={getItem(){return null;},setItem(){}};
const board=[{uid:1,id:'water',name:'water',rarity:1,base:1,value:1,coord:[0,0],neighbors:[1],tags:[]},{uid:2,id:'paper',name:'paper',rarity:1,base:1,value:1,coord:[0,1],neighbors:[0],tags:[]}];
const state={ok:true,protocol:'crucible-echoes-desktop/v1',screen:'game',status:'playing',fun_mode:'none',board,ingredients:[],items:[{id:'vault',name:'vault',description:'passive'}],essences:[{id:'vault_essence',name:'vault essence',description:'one shot'}],tokens:{},last_log:[],available_actions:['spin'],available_action_specs:[],pending_choices:[],spin:0};
let saveResolve, actionResolve, actionCalls=0;
global.window={location:{search:''},addEventListener(t,f){hooks[t]=f;},pywebview:{api:{async get_state(){return state;},async difficulty_info(){return {ok:true,rules:[]};},save(){return new Promise(resolve=>saveResolve=resolve);},action(){actionCalls++;return new Promise(resolve=>actionResolve=resolve);}}}};
vm.runInThisContext(fs.readFileSync('frontend/js/app.js','utf8'));
hooks.pywebviewready();
(async()=>{
  await new Promise(resolve=>setImmediate(resolve));
  assert.strictEqual(ids.get('board').children.length,2);
  ids.get('board').children[0].listeners.mouseenter();
  assert.strictEqual(document.querySelectorAll('.ingredient-card.selected').length,1);
  assert.strictEqual(document.querySelectorAll('.ingredient-card.neighbor').length,1);
  let prevented=false;
  ids.get('board').children[1].listeners.keydown({key:'Enter',preventDefault(){prevented=true;}});
  assert.strictEqual(prevented,true);
  assert.strictEqual(ids.get('board').children[1].classes.has('selected'),true);
  assert.strictEqual(ids.get('board').children[1].role,'button');
  assert.strictEqual(ids.get('item-actions').children.length,4); // two headings, passive item and held essence
  const menuPromise=ids.get('back-menu-btn').onclick();
  saveResolve(state);
  await menuPromise;
  assert.strictEqual(ids.get('menu-screen').classes.has('hidden'),false);
  assert.strictEqual(ids.get('game-screen').classes.has('hidden'),true);
  ids.get('end-turn-btn').onclick();
  ids.get('end-turn-btn').onclick();
  assert.strictEqual(actionCalls,1);
  actionResolve(state);
  await new Promise(resolve=>setImmediate(resolve));
  assert.strictEqual(document.body.classes.has('api-busy'),false);
  // A selected card may leave the board but remain in the pool. Details
  // must refresh from Python's new stable value, not a stale cached card.
  state.board=[board[0]];
  state.ingredients=[{uid:2,id:'paper',slot:2,stable_value:9,permanent_bonus:8,counter:3,definition:{name:'paper',rarity:1,base:1,description:'updated paper'}}];
  ids.get('end-turn-btn').onclick(); actionResolve(state);
  await new Promise(resolve=>setImmediate(resolve));
  assert.strictEqual(ids.get('details').html.includes('9g'),true);
  assert.strictEqual(ids.get('details').html.includes('updated paper'),true);
  assert.strictEqual(ids.get('pool-list').children[0].classes.has('selected'),true);
  assert.strictEqual(document.querySelectorAll('.ingredient-card.neighbor').length,0);
  state.ingredients=[];
  ids.get('end-turn-btn').onclick(); actionResolve(state);
  await new Promise(resolve=>setImmediate(resolve));
  assert.strictEqual(ids.get('detail-kind').textContent,'未选择');
  assert.strictEqual(ids.get('details').textContent.includes('已离场'),true);
  console.log('frontend regressions passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            [str(NODE_EXECUTABLE), "-e", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("frontend regressions passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
