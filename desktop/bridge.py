"""pywebview bridge for the desktop client.

The bridge is intentionally small: every mutating call delegates to the
existing GameEngine, persists the JSON save, and returns a full snapshot.  A
browser client therefore cannot bypass core legality checks or invent values.
"""

from __future__ import annotations

from copy import deepcopy
from functools import wraps
import inspect
import os
from pathlib import Path
import re
import sys
from threading import RLock
from typing import Any

from crucible_echoes.engine import GameEngine, GameError
from crucible_echoes.save import load_game, save_game

from .view_model import build_view_state
from .change_summary import action_changes


def default_save_path() -> Path:
    """Keep desktop saves writable and independent of the launch directory."""
    local_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_data) if local_data else Path.home() / ".local" / "share"
    return (base / "CrucibleEchoes" / "saves" / "current.json").resolve()


def _serialized(method):
    """pywebview runs API calls on threads; protect core state and save files."""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    # pywebview uses getfullargspec rather than signature when exposing API
    # methods. Preserve named parameters for its generated JavaScript API.
    guarded.__signature__ = inspect.signature(method)
    return guarded


class DesktopBridge:
    def __init__(self, save_path: str | Path | None = None) -> None:
        # Keep non-callable implementation objects private: pywebview walks
        # public attributes when generating the JS API and would otherwise
        # recurse into GameEngine, pathlib, or the native WebView object.
        self._lock = RLock()
        self._save_path = Path(save_path).expanduser().resolve() if save_path is not None else default_save_path()
        self._engine: GameEngine | None = None
        self._window: Any | None = None
        self._migration_error: str | None = None
        if save_path is None and not self._save_path.exists():
            self._migrate_legacy_save()

    def _migrate_legacy_save(self) -> None:
        legacy_roots = [Path.cwd(), Path(__file__).resolve().parents[1]]
        if getattr(sys, "frozen", False):
            legacy_roots.append(Path(sys.executable).resolve().parent)
        for root in legacy_roots:
            legacy = (root / ".saves" / "current.json").resolve()
            if legacy == self._save_path or not legacy.is_file():
                continue
            try:
                save_game(load_game(legacy), self._save_path)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self._migration_error = f"旧存档未能迁移（原文件保留）：{exc}"
            return

    def _empty_state(self, *, error: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "protocol": "crucible-echoes-desktop/v1",
            "ok": error is None,
            "error": error,
            "screen": "menu",
            "status": "menu",
            "available_actions": ["new_game", "continue_game", "catalog", "settings"],
            "save_path": str(self._save_path),
            "codex": {"ingredients": [], "items": [], "essences": []},
        }

    def _snapshot(self, action: str = "status", *, ok: bool = True, error: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._engine is None:
            return self._empty_state(error=error)
        return self._snapshot_for(self._engine, self._save_path, action, ok=ok, error=error)

    @staticmethod
    def _snapshot_for(engine: GameEngine, save_path: Path, action: str, *, ok: bool = True,
                      error: dict[str, Any] | None = None) -> dict[str, Any]:
        """Build a candidate view without committing it to the active game."""
        payload = engine.agent_payload(action, ok=ok, error=error)
        result = build_view_state(engine, payload)
        result["screen"] = "game"
        result["save_path"] = str(save_path)
        return result

    def _error(self, action: str, exc: Exception) -> dict[str, Any]:
        error = {"type": type(exc).__name__, "message": str(exc)}
        try:
            return self._snapshot(action, ok=False, error=error)
        except (GameError, ValueError, IndexError, TypeError, KeyError):
            # Rendering the old game must not recursively break recovery.
            # Retain its runtime and file; a later healthy view can retry.
            return self._empty_state(error=error)

    def _set_save_path(self, save_path: str | None) -> None:
        if save_path:
            self._save_path = Path(save_path).expanduser().resolve()

    @staticmethod
    def _integer_input(value: Any, label: str) -> int:
        """Never silently truncate a JS/API number or treat bool as an index.

        Decimal text also preserves seeds beyond JavaScript's safe-integer
        range. Its syntax matches the form, not Python-only separators.
        """
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise GameError(f"{label}必须是整数或十进制整数文本，不能使用小数/布尔值")
        if isinstance(value, str) and not re.fullmatch(r"[+-]?[0-9]+", value.strip()):
            raise GameError(f"{label}必须是十进制整数文本")
        return int(value)

    @_serialized
    def get_state(self) -> dict[str, Any]:
        """Return the complete current GUI snapshot."""

        try:
            if self._engine is None:
                if self._save_path.exists():
                    candidate = GameEngine().bind(load_game(self._save_path))
                    result = self._snapshot_for(candidate, self._save_path, "status")
                    self._engine = candidate
                    return result
                if self._migration_error:
                    return self._empty_state(error={"type": "SaveMigrationError", "message": self._migration_error})
                return self._empty_state()
            return self._snapshot("status")
        except (GameError, OSError, ValueError, TypeError, KeyError, IndexError) as exc:
            if self._engine is None:
                # Failed startup views never become the active game.
                return self._empty_state(error={"type": type(exc).__name__, "message": f"存档读取失败（原文件保留）：{exc}"})
            return self._error("status", exc)

    @_serialized
    def new_game(
        self,
        seed: int | str = 1,
        difficulty: int = 1,
        fun_mode: str = "none",
        save_path: str | None = None,
    ) -> dict[str, Any]:
        try:
            parsed_seed = self._integer_input(seed, "种子")
            parsed_difficulty = self._integer_input(difficulty, "难度")
            target = Path(save_path).expanduser().resolve() if save_path else self._save_path
            candidate = GameEngine()
            candidate.new_game(parsed_seed, parsed_difficulty, str(fun_mode))
            result = self._snapshot_for(candidate, target, "new")
            save_game(candidate.s, target)
            self._engine = candidate
            self._save_path = target
            self._migration_error = None
            return result
        except (GameError, ValueError, OSError, TypeError, KeyError, IndexError) as exc:
            return self._error("new", exc)

    @_serialized
    def continue_game(self, save_path: str | None = None) -> dict[str, Any]:
        try:
            target = Path(save_path).expanduser().resolve() if save_path else self._save_path
            if not target.exists():
                raise GameError(f"找不到存档：{target}")
            candidate = GameEngine().bind(load_game(target))
            result = self._snapshot_for(candidate, target, "continue")
            self._engine = candidate
            self._save_path = target
            return result
        except (GameError, ValueError, OSError, TypeError, KeyError, IndexError) as exc:
            return self._error("continue", exc)

    @_serialized
    def save(self) -> dict[str, Any]:
        try:
            if self._engine is None:
                raise GameError("当前没有正在进行的游戏")
            result = self._snapshot("save")
            save_game(self._engine.s, self._save_path)
            return result
        except (GameError, OSError, ValueError, TypeError, KeyError, IndexError) as exc:
            return self._error("save", exc)

    @staticmethod
    def _payload_value(payload: Any, key: str, default: Any = None) -> Any:
        if isinstance(payload, dict):
            return payload.get(key, default)
        return payload if payload is not None else default

    @_serialized
    def action(self, action: str, payload: Any = None) -> dict[str, Any]:
        """Apply one existing core operation and return the new full state."""

        try:
            if self._engine is None:
                raise GameError("请先新建或继续游戏")
            # Preserve transient board aliases/topology as well as persisted
            # state. Catalog definitions are read-only and can be shared.
            # Commit only after the atomic save succeeds; an error is retryable
            # without losing a turn, tokens, or the RNG trajectory.
            candidate = deepcopy(self._engine, {id(self._engine.catalog): self._engine.catalog})
            if action in {"spin", "end_turn"}:
                candidate.spin()
            elif action == "choose":
                candidate.choose(self._integer_input(self._payload_value(payload, "number", payload), "选择序号"))
            elif action == "skip":
                candidate.skip()
            elif action == "reroll":
                candidate.reroll()
            elif action == "remove":
                candidate.remove(self._integer_input(self._payload_value(payload, "index", payload), "成分序号"))
            elif action == "use":
                item_id = self._payload_value(payload, "item_id", payload)
                candidate.use_item(str(item_id))
            elif action == "toggle":
                item_id = self._payload_value(payload, "item_id", payload)
                candidate.toggle_item(str(item_id))
            else:
                raise GameError(f"未知桌面操作：{action}")
            result = self._snapshot_for(candidate, self._save_path, action)
            result["action_changes"] = action_changes(self._engine, candidate)
            try:
                save_game(candidate.s, self._save_path)
            except OSError as exc:
                return self._error(action, OSError(f"存档写入失败，本次操作未生效，原存档保留；请修复后重试：{exc}"))
            self._engine = candidate
            return result
        except (GameError, ValueError, IndexError, OSError, TypeError, KeyError) as exc:
            return self._error(action, exc)

    @_serialized
    def preview(self) -> dict[str, Any]:
        """Safely simulate the next end-turn using the real engine logic."""

        try:
            if self._engine is None:
                raise GameError("请先新建或继续游戏")
            if self._engine.s.pending:
                raise GameError("请先处理当前选择")
            preview_engine = GameEngine(self._engine.catalog).bind(deepcopy(self._engine.s))
            before = int(preview_engine.s.gold)
            income = int(preview_engine.spin())
            after_view = build_view_state(preview_engine, preview_engine.agent_payload("preview"))
            return {
                "ok": True,
                "gold_before": before,
                "gold_after": int(preview_engine.s.gold),
                "delta": int(preview_engine.s.gold) - before,
                "income": income,
                "status": preview_engine.s.status,
                "spins_left": preview_engine.s.spins_left,
                "log": list(preview_engine.s.last_log),
                "board": after_view.get("board", []),
            }
        except (GameError, ValueError, IndexError, TypeError, KeyError) as exc:
            return {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}

    @_serialized
    def catalog(self) -> dict[str, Any]:
        if self._engine is None:
            return self._empty_state()
        state = self._snapshot("catalog")
        state["screen"] = "codex"
        return state.get("codex", {})

    @_serialized
    def difficulty_info(self, difficulty: int = 1) -> dict[str, Any]:
        """Expose cumulative declarative difficulty rules for the setup screen."""

        try:
            value = self._integer_input(difficulty, "难度")
            if not 1 <= value <= 15:
                raise GameError("难度必须在1到15之间")
            catalog = self._engine.catalog if self._engine is not None else GameEngine().catalog
            rules = [
                {"difficulty": int(threshold), "rule": dict(rule)}
                for threshold, rule in sorted(catalog.progression.get("difficulty", {}).items(), key=lambda row: int(row[0]))
                if value >= int(threshold)
            ]
            return {"ok": True, "difficulty": value, "rules": rules}
        except (GameError, ValueError, TypeError) as exc:
            return {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}

    @_serialized
    def close(self) -> dict[str, Any]:
        """Save and request a graceful window close when running under pywebview."""

        result = self.save() if self._engine is not None else self._empty_state()
        if self._window is not None and result.get("ok"):
            self._window.destroy()
        return result
