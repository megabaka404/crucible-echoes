from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable

from .catalog import Catalog
from .geometry import adjacent_indices, board_coords, is_corner, is_edge, orthogonal_indices
from .model import GameState, IngredientInstance, PendingChoice
from .rng import DeterministicRNG


class GameError(RuntimeError):
    pass


MINERAL_TAGS = frozenset({"stone", "ore", "metal"})

RUN_END_OPTIONS = {
    "end_run": {
        "id": "end_run",
        "name": "结束本局",
        "description": "按正常通关处理，结束本局。",
    },
    "enter_endless": {
        "id": "enter_endless",
        "name": "进入无限模式",
        "description": "保留当前状态，进入10回合的无限订单1。",
    },
    "enter_peace": {
        "id": "enter_peace",
        "name": "进入和平模式",
        "description": "保留当前状态，进入7回合、0g的和平订单，达到1000000g时胜利。",
    },
}

PEACE_MODE_TARGET = 1_000_000
FUN_MODES = ("none", "giant", "rapid", "blind_box", "minimal", "mutation")


class GameEngine:
    def __init__(self, catalog: Catalog | None = None):
        self.catalog = catalog or Catalog.load()
        self.state: GameState | None = None
        self.rng: DeterministicRNG | None = None
        self._round_events: defaultdict[str, int] = defaultdict(int)
        self._round_event_values: defaultdict[str, int] = defaultdict(int)
        self._board: list[IngredientInstance] = []
        self._coords: list[tuple[int, int]] = []
        self._values: list[int] = []
        self._all_adjacent = False
        self._panorama = False
        self._removed_values: list[tuple[int, int]] = []
        self._triggering_essences: set[str] = set()

    def new_game(self, seed: int, difficulty: int = 1, fun_mode: str = "none") -> GameState:
        if not 1 <= difficulty <= 15:
            raise GameError("难度必须在1到15之间")
        if fun_mode not in FUN_MODES:
            raise GameError(f"娱乐模式必须是：{', '.join(FUN_MODES)}")
        self.rng = DeterministicRNG(seed & ((1 << 64) - 1))
        self._round_events = defaultdict(int)
        self._round_event_values = defaultdict(int)
        self._removed_values = []
        self._board = []
        self._coords = []
        self._values = []
        self._all_adjacent = False
        self._panorama = False
        first = self.current_order_for(0, difficulty, {}, fun_mode=fun_mode)
        state = GameState(
            version=1,
            seed=seed,
            rng_state=self.rng.state,
            difficulty=difficulty,
            gold=int(self.catalog.progression["starting_gold"]),
            status="playing",
            spin=0,
            order_index=0,
            spins_left=first[1],
            next_uid=1,
            stats={
                "event_counts": {},
                "event_values": {},
                "round_events": {},
                "round_event_values": {},
                "round_removed_values": [],
                "essence_baseline": {},
                "essence_hits": {},
                "seen_types": [],
                "observed_content": {"ingredients": [], "items": [], "essences": []},
                "spawn_counters": {},
                "item_event_counts": {},
                "item_trigger_counts": {},
                "item_storage": {},
                "endless_orders_completed": 0,
                "highest_endless_order": 0,
                "highest_endless_single_turn_gold": 0,
                "highest_single_turn_gold": 0,
                "minimal_successful_orders": 0,
            },
            flags={
                "choice_minimum_count": 0,
                "choice_minimum_rarity": 0,
                "choice_minimum_reserved": 0,
                "ingredient_generation_disabled": False,
                "ingredient_generation_permanently_disabled": False,
                "ingredient_generation_bonus": 0,
                "global_permanent_bonuses": {},
            },
            endless_mode=False,
            endless_order=0,
            endless_target=0,
            peace_mode=False,
            peace_order=0,
            fun_mode=fun_mode,
        )
        self.state = state
        for def_id in self.catalog.progression["initial_ingredients"]:
            self.add_ingredient(def_id, emit=False)
        if fun_mode == "minimal":
            # Minimal mode's opening trim is deliberately outside the normal
            # remove pipeline: no on-remove rewards or listeners should fire
            # before the first spin, but the deterministic RNG still records
            # the choice in the saved rng_state.
            initial = list(state.ingredients)
            for instance in self.r.sample(initial, min(2, len(initial))):
                state.ingredients = [x for x in state.ingredients if x.uid != instance.uid]
            state.last_log.append("极简模式：开局随机裁剪2个初始成分。")
        for _ in range(self.initial_slag_count(difficulty)):
            self.add_ingredient("slag", emit=False)
        state.last_log.insert(0, f"新实验开始：seed={seed}，难度={difficulty}，娱乐模式={fun_mode}。")
        self._sync_rng()
        return state

    def bind(self, state: GameState) -> "GameEngine":
        self.state = state
        # Older saves predate the generic generation counters.  Keep the
        # counters inside the existing stats object so no new required JSON
        # dataclass field is introduced.
        self.s.stats.setdefault("spawn_counters", {})
        self.s.stats.setdefault("event_counts", {})
        self.s.stats.setdefault("event_values", {})
        self.s.stats.setdefault("round_events", {})
        self.s.stats.setdefault("round_event_values", {})
        self.s.stats.setdefault("round_removed_values", [])
        self.s.stats.setdefault("essence_baseline", {})
        self.s.stats.setdefault("essence_hits", {})
        self.s.stats.setdefault("seen_types", [])
        self.s.stats.setdefault("item_event_counts", {})
        self.s.stats.setdefault("item_trigger_counts", {})
        self.s.stats.setdefault("item_storage", {})
        self.s.stats.setdefault("endless_orders_completed", 0)
        self.s.stats.setdefault("highest_endless_order", 0)
        self.s.stats.setdefault("highest_endless_single_turn_gold", 0)
        self.s.stats.setdefault("highest_single_turn_gold", 0)
        self.s.stats.setdefault("minimal_successful_orders", 0)
        self.s.flags.setdefault("choice_minimum_count", 0)
        self.s.flags.setdefault("choice_minimum_rarity", 0)
        self.s.flags.setdefault("choice_minimum_reserved", 0)
        self.s.flags.setdefault("ingredient_generation_disabled", False)
        self.s.flags.setdefault("ingredient_generation_permanently_disabled", False)
        self.s.flags.setdefault("ingredient_generation_bonus", 0)
        self.s.flags.setdefault("global_permanent_bonuses", {})
        if getattr(self.s, "fun_mode", None) not in FUN_MODES:
            self.s.fun_mode = "none"
        self.rng = DeterministicRNG(state.rng_state)
        # Round counters are persisted because the agent interface executes
        # one action per process. Without restoring them, two rerolls issued
        # in separate processes would look like two unrelated first rerolls.
        self._round_events = defaultdict(int, {
            str(key): int(value)
            for key, value in self.s.stats.get("round_events", {}).items()
        })
        self._round_event_values = defaultdict(int, {
            str(key): int(value)
            for key, value in (self.s.stats.get("round_event_values") or {}).items()
        })
        self._removed_values = [
            (int(row[0]), int(row[1]))
            for row in (self.s.stats.get("round_removed_values") or [])
        ]
        self._restore_board_snapshot()
        self._remember_visible_content()
        return self

    def _restore_board_snapshot(self) -> None:
        """Rehydrate the last draw without repeating settlement or RNG draws.

        Pool instances are authoritative for presence and mechanism state.
        Departed instances stay as placeholders, retaining their coordinates
        so an empty slot cannot make formerly distant survivors adjacent.
        """
        self._board = []
        self._coords = []
        self._values = []
        live = {instance.uid: instance for instance in self.s.ingredients}
        layout = board_coords(self.s.expanded, self.s.fun_mode)
        for row in self.s.last_board:
            uid = int(row["uid"])
            instance = live.get(uid)
            if instance is None:
                def_id = str(row.get("id", ""))
                if def_id not in self.catalog.ingredients:
                    continue
                instance = IngredientInstance(uid=uid, def_id=def_id)
            coord = row.get("coord")
            slot_index = int(row.get("slot", len(self._board) + 1)) - 1
            if not isinstance(coord, (list, tuple)) or len(coord) != 2:
                if not 0 <= slot_index < len(layout):
                    continue
                coord = layout[slot_index]
            self._board.append(instance)
            self._coords.append((int(coord[0]), int(coord[1])))
            self._values.append(int(row.get("value", 0)))
        topology = self.s.stats.get("last_board_topology", {})
        # Legacy saves cannot recover an already-decayed one-spin flag, but
        # periodic item topology can be derived from the recorded spin.
        self._all_adjacent = bool(topology.get(
            "all_adjacent", "global_reaction_field" in self.s.items and self.s.spin > 0 and self.s.spin % 3 == 0
        )) if self._board else False
        self._panorama = bool(topology.get(
            "panorama", "panorama_mirror" in self.s.items and self.s.spin > 0 and self.s.spin % 3 == 0
        )) if self._board else False

    @property
    def s(self) -> GameState:
        if self.state is None:
            raise GameError("尚未载入游戏")
        return self.state

    @property
    def r(self) -> DeterministicRNG:
        if self.rng is None:
            raise GameError("随机数生成器尚未初始化")
        return self.rng

    def _sync_rng(self) -> None:
        self._remember_visible_content()
        self.s.rng_state = self.r.state
        self.s.stats["round_events"] = dict(self._round_events)
        self.s.stats["round_event_values"] = dict(self._round_event_values)
        self.s.stats["round_removed_values"] = [list(row) for row in self._removed_values]

    def _remember_visible_content(self) -> None:
        """Persist discoveries without events, draws, or definition copies.

        Only published state counts: internal rejected draw trials never enter
        the pending queue. The Agent exposes all queued rewards, not just the
        GUI's frontmost one. History is per-save, not a global unlock system.
        """
        history = self.s.stats.setdefault("observed_content", {})
        visible = {
            "ingredients": [x.def_id for x in self.s.ingredients] + list(self.s.removed_history)
                           + list(self.s.stats.get("seen_types", [])),
            "items": list(self.s.items),
            "essences": list(self.s.essences) + list(self.s.consumed_essences),
        }
        groups = {"ingredient": "ingredients", "item": "items", "essence": "essences"}
        for choice in self.s.pending:
            if choice.kind in groups:
                visible[groups[choice.kind]].extend(choice.offers)
        for group, ids in visible.items():
            known = set(history.get(group, []))
            known.update(def_id for def_id in ids if def_id in getattr(self.catalog, group))
            history[group] = sorted(known)

    @staticmethod
    def initial_slag_count(difficulty: int) -> int:
        if difficulty >= 8:
            return 3
        if difficulty >= 6:
            return 2
        if difficulty >= 5:
            return 1
        return 0

    @staticmethod
    def slag_interval(difficulty: int, progression: dict[str, Any] | None = None) -> int | None:
        """Resolve the cumulative periodic slag interval from difficulty data.

        ``progression`` is optional to retain the small public helper used by
        older callers/tests; normal engine execution passes its loaded
        catalog so custom catalogs remain data-driven.
        """
        if progression is None:
            progression = Catalog.load().progression
        interval: int | None = None
        for threshold, rule in progression.get("difficulty", {}).items():
            if difficulty < int(threshold):
                continue
            if "slag_interval" in rule:
                value = rule.get("slag_interval")
                interval = None if value is None else max(1, int(value))
        return interval

    def current_order_for(
        self,
        completed: int,
        difficulty: int,
        flags: dict[str, Any],
        *,
        fun_mode: str | None = None,
        endless: bool = False,
        peace: bool = False,
    ) -> tuple[int, int]:
        if fun_mode is None:
            fun_mode = getattr(self.state, "fun_mode", "none")
        rows = self.catalog.progression["orders"]
        if completed < len(rows):
            amount = int(rows[completed]["amount"])
            spins = int(rows[completed]["spins"])
        else:
            amount = 1000 + 500 * (completed - 12)
            spins = 10
        number = completed + 1
        for threshold, rule in self.catalog.progression.get("difficulty", {}).items():
            if difficulty < int(threshold):
                continue
            for order_number, bonus in rule.get("order_bonus", {}).items():
                if int(order_number) == number:
                    amount += int(bonus)
        final_order = self._final_order_for(completed, difficulty)
        if final_order:
            amount = int(final_order["amount"])
            spins = int(final_order["spins"])
        if flags.get("next_order_penalty"):
            amount = (amount * 5 + 3) // 4
        amount = self.apply_fun_order_modifier(
            amount,
            number,
            fun_mode=fun_mode,
            endless=endless,
            peace=peace,
        )
        return amount, spins

    @staticmethod
    def apply_fun_order_modifier(
        amount: int,
        order_number: int,
        *,
        fun_mode: str = "none",
        endless: bool = False,
        peace: bool = False,
    ) -> int:
        """Apply entertainment-mode order changes after normal difficulty.

        Peace orders are explicitly zero and endless orders are exempt from
        blind-box's mainline discount. Giant mode multiplies every payable
        target, including endless and difficulty-generated final orders.
        """
        resolved = max(0, int(amount))
        if peace:
            return 0
        if fun_mode == "blind_box" and not endless and int(order_number) >= 4:
            resolved = (resolved * 85 + 99) // 100  # ceil(resolved * 0.85)
        if fun_mode == "giant":
            resolved = (resolved * 7 + 3) // 4  # ceil(resolved * 1.75)
        return resolved

    def board_capacity(self) -> int:
        base = 40 if self.s.fun_mode == "giant" else 12 if self.s.fun_mode == "minimal" else 20
        return base + (1 if self.s.expanded else 0)

    def _final_order_for(self, completed: int, difficulty: int) -> dict[str, Any] | None:
        """Return an active declarative extra-order rule, if any."""
        result: dict[str, Any] | None = None
        for threshold, rule in self.catalog.progression.get("difficulty", {}).items():
            if difficulty < int(threshold):
                continue
            candidate = rule.get("final_order")
            if candidate and int(candidate.get("after_completed", -1)) == completed:
                result = candidate
        return result

    def token_reward_amounts(self, difficulty: int) -> dict[str, int]:
        """Resolve cumulative token rewards, including legacy integer rules."""
        amounts = {"remove": 2, "roll": 2, "essence": 2}
        for threshold, rule in self.catalog.progression.get("difficulty", {}).items():
            if difficulty < int(threshold) or "token_reward" not in rule:
                continue
            reward = rule["token_reward"]
            if isinstance(reward, dict):
                for token, amount in reward.items():
                    if token in amounts:
                        amounts[token] = int(amount)
            else:
                amounts = {token: int(reward) for token in amounts}
        return amounts

    def current_order(self) -> tuple[int, int]:
        if self.s.peace_mode:
            return 0, 7
        if self.s.endless_mode:
            amount = int(self.s.endless_target)
            if self.s.flags.get("next_order_penalty"):
                amount = (amount * 5 + 3) // 4
            return self.apply_fun_order_modifier(
                amount,
                int(self.s.endless_order),
                fun_mode=self.s.fun_mode,
                endless=True,
            ), 10
        return self.current_order_for(
            self.s.order_index,
            self.s.difficulty,
            self.s.flags,
            fun_mode=self.s.fun_mode,
        )

    def difficulty_ingredient_override(self, def_id: str) -> dict[str, Any]:
        """Merge declarative difficulty overrides for one ingredient ID."""
        overrides: dict[str, Any] = {}
        for threshold, rule in self.catalog.progression.get("difficulty", {}).items():
            if self.s.difficulty < int(threshold):
                continue
            candidate = rule.get("ingredient_overrides", {}).get(def_id)
            if isinstance(candidate, dict):
                overrides.update(candidate)
        return overrides

    def post_order_gold_deduction(self, completed: int) -> int:
        """Return any cumulative post-settlement deduction for mainline orders."""
        amount = 0
        for threshold, rule in self.catalog.progression.get("difficulty", {}).items():
            if self.s.difficulty < int(threshold):
                continue
            spec = rule.get("post_order_gold_deduction")
            if isinstance(spec, dict) and completed >= int(spec.get("from_completed", 0)):
                amount = max(amount, int(spec.get("amount", 0)))
        return max(0, amount)

    def _check_peace_goal(self) -> bool:
        if not self.s.peace_mode or self.s.gold < PEACE_MODE_TARGET:
            return False
        self.s.status = "won"
        self.s.pending.clear()
        self.s.last_log.append("和平模式已达到1000000g：本局胜利。")
        return True

    @staticmethod
    def next_endless_target(target: int) -> int:
        """Return ceil(target * 1.5) using integer arithmetic."""
        if target <= 0:
            return 1000
        return (int(target) * 3 + 1) // 2

    def _mainline_complete(self, completed: int, difficulty: int) -> bool:
        """Whether the just-completed order is the final normal-line order."""
        if completed < 12:
            return False
        # D10+ use a declarative 15-spin final order, completed at order 13.
        return self._final_order_for(completed, difficulty) is None

    def rarity_table(self, kind: str) -> list[float]:
        completed = self.s.order_index
        source = self.catalog.progression[f"{kind}_rarity"]
        if str(completed) in source:
            base = list(map(float, source[str(completed)]))
        else:
            cap = 5 if kind == "ingredient" else 6
            base = list(map(float, source[f"{cap}+"]))
        multiplier = self.current_rarity_multiplier()
        high = [base[1] * multiplier, base[2] * multiplier, base[3] * multiplier]
        remaining = 100.0
        result = [0.0, 0.0, 0.0, 0.0]
        for index in (3, 2, 1):
            value = min(remaining, high[index - 1])
            result[index] = max(0.0, value)
            remaining -= result[index]
        result[0] = max(0.0, remaining)
        return result

    def current_rarity_multiplier(self) -> float:
        multiplier = float(self.s.rarity_multiplier)
        for threshold, rule in self.catalog.progression.get("difficulty", {}).items():
            if self.s.difficulty >= int(threshold):
                multiplier *= float(rule.get("rarity_weight_multiplier", 1.0))
        disable_negative = self.negative_disabled()
        for instance in self.s.ingredients:
            definition = self.catalog.ingredients[instance.def_id]
            if disable_negative and "negative" in definition.get("tags", []):
                continue
            multiplier *= float(definition.get("rarity_multiplier", 1.0))
        for item_id in self.s.items:
            multiplier *= float(self.catalog.items[item_id].get("rarity_multiplier", 1.0))
        return multiplier

    def negative_disabled(self) -> bool:
        return "holy_water" in self.s.items or any(x.def_id == "white_mage" for x in self.s.ingredients)

    @staticmethod
    def ingredient_has_generation(definition: dict[str, Any]) -> bool:
        """Return whether a definition owns a component-generation effect.

        Declarative spawn fields are recognised automatically.  Scripted
        definitions opt in with ``ingredient_generation`` so the ban item and
        its permanent essence can share one rule without scattering card IDs
        through the engine.
        """
        if "ingredient_generation" in definition:
            return bool(definition.get("ingredient_generation"))
        if any(definition.get(field) for field in ("periodic_spawn", "chance_spawn", "spawn_each_spin")):
            return True
        on_removed = definition.get("on_removed")
        if isinstance(on_removed, dict) and on_removed.get("spawn_tag"):
            return True
        potion = definition.get("potion")
        return isinstance(potion, dict) and bool(potion.get("recycle"))

    def ingredient_generation_disabled(self) -> bool:
        """Whether component-owned generation is currently suppressed."""
        return bool(
            self.s.flags.get("ingredient_generation_disabled", False)
            or self.s.flags.get("ingredient_generation_permanently_disabled", False)
        )

    def _generated_ingredient(self, def_id: str, *, source: int | None = None) -> IngredientInstance | None:
        """Add one ingredient from a component-owned generation effect.

        All such additions pass through this helper so the ban state and the
        generated-event counter (including the ban essence) stay consistent.
        """
        return self._spawn_random(def_id=def_id, source=source, origin="ingredient")

    def _after_successful_generation(self, created: IngredientInstance, *, source: int | None, origin: str) -> None:
        """Shared post-generation effects for both fixed and random sources.

        Ordinary gains do not enter this gateway. The extra generation comes
        from an essence, not a component, so a component ban cannot suppress
        it. The legacy flag name is retained for existing saves.
        """
        self.emit("generated", source_index=source)
        if source is not None and 0 <= source < len(self._board):
            for n in self._neighbors(source):
                target = self._board[n]
                if self._present(target) and target.def_id == "proliferation_core" and not target.flags.get(f"proliferated:{self.s.spin}"):
                    self._permanent_bonus(target, 1)
                    target.flags[f"proliferated:{self.s.spin}"] = True
        if origin == "ingredient" and "double_cauldron" in self.s.items and not self._round_events.get("double_cauldron"):
            self._round_events["double_cauldron"] = 1
            self._gain_gold(3, "双层坩埚")
            self._record_item_trigger("double_cauldron")
        if self.s.flags.pop("copy_next_generation", False):
            rarity = int(self.catalog.ingredients[created.def_id].get("rarity", 0))
            candidates = self._defs_at_rarity("ingredient", rarity)
            if candidates:
                # Exactly the same tier, with the normal legal-definition
                # filters/weights. Never fall back to another tier or emit a
                # copied event for an acquisition that did not happen.
                extra_id = self.r.weighted_choice([(row["id"], float(row.get("pool_weight", 1.0))) for row in candidates])
                self._spawn_random(def_id=extra_id, origin="essence")

    def _apply_generation_bonus_to_instance(self, instance: IngredientInstance) -> None:
        """Materialize the permanent ban-essence bonus on one generator.

        The global amount remains in flags for future definitions and save
        inspection; the per-instance marker prevents the calculated value from
        double-counting a bonus already materialized here.
        """
        if not self.s.flags.get("ingredient_generation_permanently_disabled"):
            return
        if not self.ingredient_has_generation(self.catalog.ingredients[instance.def_id]):
            return
        total = int(self.s.flags.get("ingredient_generation_bonus", 0))
        applied = int(instance.flags.get("ingredient_generation_bonus_applied", 0))
        if total > applied:
            delta = total - applied
            # The ban essence materialises its global +1g as a permanent
            # value bonus on each generator.  Minimal mode doubles all such
            # permanent growth at this single shared boundary as well.
            if self.s.fun_mode == "minimal":
                delta *= 2
            instance.permanent_bonus += delta
            instance.flags["ingredient_generation_bonus_applied"] = total

    def _potion_effect_multiplier(self) -> int:
        multiplier = 1
        for item_id in self.s.items:
            multiplier *= max(1, int(self.catalog.items[item_id].get("potion_effect_multiplier", 1)))
        return multiplier

    def roll_rarity(self, kind: str, minimum: int = 1, maximum: int = 4) -> int:
        table = self.rarity_table(kind)
        weighted = [(rarity, table[rarity - 1]) for rarity in range(minimum, maximum + 1)]
        if sum(weight for _, weight in weighted) <= 0:
            available = [rarity for rarity in range(minimum, maximum + 1) if self._defs_at_rarity(kind, rarity)]
            return self.r.choice(available)
        return self.r.weighted_choice(weighted)

    def _defs_at_rarity(self, kind: str, rarity: int, *, tag: str | None = None, exclude: set[str] | None = None) -> list[dict[str, Any]]:
        collection = self.catalog.ingredients if kind == "ingredient" else self.catalog.items
        rows = []
        for row in collection.values():
            if int(row.get("rarity", 0)) != rarity:
                continue
            if not row.get("offerable", True):
                continue
            if row.get("unique") and row["id"] in self.s.acquired_once:
                continue
            if kind == "item" and row["id"] in self.s.items:
                continue
            if exclude and row["id"] in exclude:
                continue
            if tag and tag not in row.get("tags", []):
                continue
            rows.append(row)
        return rows

    def _draw_candidates(self, kind: str, rarity: int, *, tag: str | None = None, exclude: set[str] | None = None, minimum: int = 1, maximum: int = 4) -> list[dict[str, Any]]:
        """Resolve fallback tiers without leaving an explicit reward bound.

        The default range preserves the historical downward-then-upward
        order. Minimum/fixed rewards and generation pass their own bounds;
        exhausting a tier must not silently invalidate those constraints.
        This eligibility check never draws RNG or emits choice events.
        """
        rows = self._defs_at_rarity(kind, rarity, tag=tag, exclude=exclude)
        if not minimum <= rarity <= maximum:
            rows = []
        if not rows:
            for fallback in range(min(rarity - 1, maximum), minimum - 1, -1):
                rows = self._defs_at_rarity(kind, fallback, tag=tag, exclude=exclude)
                if rows:
                    break
        if not rows:
            # Some tagged families intentionally have no definitions at every
            # rarity (for example, the ore tag starts at rarity 2). Preserve
            # the requested minimum by falling upward before failing.
            for fallback in range(max(rarity + 1, minimum), maximum + 1):
                rows = self._defs_at_rarity(kind, fallback, tag=tag, exclude=exclude)
                if rows:
                    break
        if not rows and tag:
            # Only leave the requested tag family as a last resort.  This is
            # needed for families that have no definitions at any rarity, but
            # must not turn an ore generation into an unrelated special card
            # just because the weighted rarity landed on an empty tier.
            rows = self._defs_at_rarity(kind, rarity, exclude=exclude) if minimum <= rarity <= maximum else []
            if not rows:
                for fallback in list(range(min(rarity - 1, maximum), minimum - 1, -1)) + list(range(max(rarity + 1, minimum), maximum + 1)):
                    rows = self._defs_at_rarity(kind, fallback, exclude=exclude)
                    if rows:
                        break
        return rows

    def _draw_definition(self, kind: str, rarity: int, *, tag: str | None = None, exclude: set[str] | None = None, minimum: int = 1, maximum: int = 4) -> str:
        rows = self._draw_candidates(kind, rarity, tag=tag, exclude=exclude, minimum=minimum, maximum=maximum)
        if not rows:
            raise GameError(f"{kind}池中没有可抽取定义")
        return self.r.weighted_choice([(row["id"], float(row.get("pool_weight", 1.0))) for row in rows])

    def make_choice(self, kind: str, count: int = 3, *, minimums: list[int] | None = None, fixed_rarity: int | None = None, source: str = "spin", can_skip: bool = True, guarantee_rarity: int | None = None, tag_filter: str | None = None, _redraw: bool = False) -> PendingChoice:
        # A reroll replaces candidates in the same reward group. Its count
        # already includes acquisition bonuses; next-choice flags and hooks
        # belong to the next newly awarded group, not this redraw.
        if not _redraw:
            self._trigger_context_essences("before_choice", kind=kind)
        if kind == "essence":
            rows = [row for row in self.catalog.essences.values() if row["id"] not in self.s.essences and row["id"] not in self.s.consumed_essences]
            self.r.shuffle(rows)
            offers = [row["id"] for row in rows[:count]]
            return PendingChoice(kind="essence", offers=offers, can_skip=can_skip, source=source)
        extra = 0
        choice_guarantee: int | None = guarantee_rarity
        if kind == "ingredient" and not _redraw:
            extra += int(self.s.flags.pop("ingredient_choice_extra", 0))
            if self.s.flags.get("credit_card_bonus"):
                extra += int(self.s.flags.pop("credit_card_bonus"))
            if self.s.flags.pop("blank_choice", False):
                count = 1
            if self.s.flags.pop("mundane_choice", False):
                fixed_rarity = 1
            force_rarity4 = self.s.flags.pop("force_rarity4", False)
            guaranteed_minimum = int(self.s.flags.get("choice_minimum_rarity", 0))
            guaranteed_count = int(self.s.flags.get("choice_minimum_count", 0))
            reserved_count = int(self.s.flags.get("choice_minimum_reserved", 0))
            if self.s.flags.pop("lucky_choice", False):
                guaranteed_minimum = max(guaranteed_minimum, 3)
                guaranteed_count = max(guaranteed_count, 1)
                self.s.flags["choice_minimum_rarity"] = guaranteed_minimum
                self.s.flags["choice_minimum_count"] = guaranteed_count
            if guarantee_rarity is not None:
                minimums = list(minimums or [])
                if minimums:
                    minimums[0] = max(minimums[0], guarantee_rarity)
                else:
                    minimums = [guarantee_rarity]
            elif force_rarity4:
                minimums = [4]
                if guaranteed_count > reserved_count:
                    choice_guarantee = max(4, guaranteed_minimum)
                    self.s.flags["choice_minimum_reserved"] = reserved_count + 1
            else:
                if guaranteed_count > reserved_count and guaranteed_minimum > 0 and (
                    fixed_rarity is None or fixed_rarity >= guaranteed_minimum
                ):
                    minimums = list(minimums or [])
                    if fixed_rarity is None:
                        if minimums:
                            minimums[0] = max(minimums[0], guaranteed_minimum)
                        else:
                            minimums = [guaranteed_minimum]
                    choice_guarantee = guaranteed_minimum
                    self.s.flags["choice_minimum_reserved"] = reserved_count + 1
        elif kind == "ingredient" and guarantee_rarity is not None:
            minimums = list(minimums or [])
            if minimums:
                minimums[0] = max(minimums[0], guarantee_rarity)
            else:
                minimums = [guarantee_rarity]
        elif kind == "item" and not _redraw:
            extra += int(self.s.flags.pop("item_choice_extra", 0))
            extra += sum(int(self.catalog.items[item].get("item_choice_bonus", 0)) for item in self.s.items)
        count += extra
        if kind == "item":
            # Owned/non-repeatable items can exhaust during infinite runs.
            # Offer the remaining distinct legal items rather than attempting
            # a third draw from an empty pool. Ordinary stocked pools retain
            # their exact draw order and RNG calls.
            available = {row["id"] for rarity in range(1, 5)
                         for row in self._defs_at_rarity("item", rarity)}
            count = min(count, len(available))
        minimums = list(minimums or [])
        offers: list[str] = []
        for index in range(count):
            minimum = fixed_rarity if fixed_rarity is not None else (minimums[index] if index < len(minimums) else 1)
            maximum = fixed_rarity if fixed_rarity is not None else 4
            if not self._draw_candidates(kind, minimum, tag=tag_filter, exclude=set(offers), minimum=minimum, maximum=maximum):
                # A constrained reward may run out before the global pool.
                # Retain only legal candidates, or a skippable empty reward.
                break
            if fixed_rarity:
                rarity = fixed_rarity
            elif index < len(minimums):
                rarity = self.roll_rarity(kind, minimum=minimums[index])
            else:
                rarity = self.roll_rarity(kind)
            offers.append(self._draw_definition(kind, rarity, tag=tag_filter, exclude=set(offers), minimum=minimum, maximum=maximum))
        if kind == "ingredient" and "lucky_compass" in self.s.items and offers:
            slot = self.r.randint(0, len(offers) - 1)
            table = self.rarity_table("ingredient")
            multiplier = float(self.catalog.items["lucky_compass"].get("candidate_rarity_weight", 1.0))
            minimum = minimums[slot] if slot < len(minimums) else 1
            boosted = [(rarity, table[rarity - 1] * (multiplier if rarity >= 2 else 1.0)) for rarity in range(minimum, 5)]
            if sum(weight for _, weight in boosted) > 0:
                rarity = self.r.weighted_choice(boosted)
            else:
                available = [tier for tier in range(minimum, 5) if self._defs_at_rarity("ingredient", tier, tag=tag_filter)]
                rarity = self.r.choice(available)
            # Keep the existing draw, but fixed-tier rewards remain fixed.
            if fixed_rarity is not None:
                rarity = fixed_rarity
            offers[slot] = self._draw_definition("ingredient", rarity, tag=tag_filter, exclude=set(offers[:slot] + offers[slot + 1:]), minimum=(fixed_rarity if fixed_rarity is not None else minimum), maximum=(fixed_rarity if fixed_rarity is not None else 4))
        choice = PendingChoice(kind=kind, offers=offers, can_skip=can_skip or not offers, source=source, minimum_rarity=choice_guarantee, tag_filter=tag_filter)
        if fixed_rarity is not None or minimums:
            choice.details["draw_constraints"] = {"fixed_rarity": fixed_rarity, "minimums": minimums}
        if not _redraw:
            self._record_choice_events(choice)
        return choice

    def _record_choice_events(self, choice: PendingChoice) -> None:
        if choice.kind not in {"ingredient", "item"}:
            return
        collection = self.catalog.ingredients if choice.kind == "ingredient" else self.catalog.items
        rarities = [int(collection[x]["rarity"]) for x in choice.offers]
        if rarities and all(x == 1 for x in rarities):
            self.emit("all_common_choice")
            if "probability_calibrator" in self.s.items:
                self._gain_gold(5, "概率校准器")
        self.s.stats["last_choice_rarities"] = rarities

    def add_ingredient(
        self,
        def_id: str,
        *,
        emit: bool = True,
        permanent_bonus: int = 0,
        _mode_processed: bool = False,
        _fun_mode_copy: bool = False,
    ) -> IngredientInstance | None:
        """Gain one ingredient through the shared entertainment-mode gateway.

        Blind-box replacement happens once before the actual gain; giant mode
        then creates exactly one non-recursive copy of the resolved identity.
        Internal callers may pass ``_mode_processed`` only for that copy (or
        other engine-level recursive mechanics), never for user-facing gains.
        """
        if not _mode_processed:
            actual_id = def_id
            if self.s.fun_mode == "blind_box":
                actual_id = self._draw_definition(
                    "ingredient", self.roll_rarity("ingredient")
                )
            created = self.add_ingredient(
                actual_id,
                emit=emit,
                permanent_bonus=permanent_bonus,
                _mode_processed=True,
            )
            if created is not None and self.s.fun_mode == "giant":
                self.add_ingredient(
                    actual_id,
                    emit=emit,
                    permanent_bonus=permanent_bonus,
                    _mode_processed=True,
                    _fun_mode_copy=True,
                )
            return created
        definition = self.catalog.ingredients[def_id]
        if definition.get("unique") and def_id in self.s.acquired_once and not _fun_mode_copy:
            return None
        if definition.get("on_acquire") == "expand_board":
            self.s.expanded = True
            if def_id not in self.s.acquired_once:
                self.s.acquired_once.append(def_id)
            self.s.last_log.append("工程图纸自动展开：实验台永久增加至21格。")
            return None
        # A declarative initial counter lets time-based ingredients start with
        # partial progress.  ``age`` is the progress used by legacy scripts,
        # while ``counter`` is the generic periodic counter; seeding both
        # keeps the two persisted views consistent for definitions that use
        # either representation.  Definitions without the field retain the
        # historical zero-based initialization.
        initial_counter = int(definition.get("initial_counter", 0))
        instance = IngredientInstance(
            uid=self.s.next_uid,
            def_id=def_id,
            permanent_bonus=permanent_bonus,
            age=initial_counter,
            counter=initial_counter,
        )
        self.s.next_uid += 1
        self.s.ingredients.append(instance)
        self._apply_generation_bonus_to_instance(instance)
        if definition.get("unique") and def_id not in self.s.acquired_once:
            self.s.acquired_once.append(def_id)
        if emit:
            self.emit("ingredient_added")
            seen = set(self.s.stats.setdefault("seen_types", []))
            if def_id not in seen:
                self.s.stats["seen_types"].append(def_id)
                self.emit("new_ingredient_type")
                if "refined_catalog" in self.s.items:
                    self._gain_gold(4, "精炼目录")
            rarity = int(definition.get("rarity", 0))
            if rarity >= 3:
                self.emit("chosen_rare")
            if "venture_capital" in self.s.items:
                self._gain_gold(8 if rarity >= 3 else (-1 if rarity == 1 else 0), "风险投资")
            if "animal" in definition.get("tags", []) and "animal_registry" in self.s.items:
                key = f"animal_seen:{def_id}"
                if not self.s.stats.get(key):
                    self.s.stats[key] = True
                    reward = int(self.catalog.items["animal_registry"].get("first_animal_gold", 6))
                    self._gain_gold(reward, "动物登记册")
                    self.emit("new_animal")
        return instance

    def gain_ingredient(self, def_id: str, *, emit: bool = True, permanent_bonus: int = 0) -> IngredientInstance | None:
        """Explicit alias for the shared ingredient-gain entry point."""
        return self.add_ingredient(def_id, emit=emit, permanent_bonus=permanent_bonus)

    def add_item(self, item_id: str) -> None:
        data = self.catalog.items[item_id]
        if item_id in self.s.items or (data.get("unique") and item_id in self.s.acquired_once):
            return
        self.s.items.append(item_id)
        if data.get("unique") and item_id not in self.s.acquired_once:
            self.s.acquired_once.append(item_id)
        self.s.last_log.append(f"获得道具：{self.catalog.items[item_id]['name']}。")
        acquire = data.get("on_acquire", {})
        for _ in range(int(acquire.get("ingredient_choices", 0))):
            self.s.pending.append(self.make_choice("ingredient", source=item_id))
        for rarity in acquire.get("fixed_ingredient_choices", []):
            self.s.pending.append(self.make_choice("ingredient", fixed_rarity=int(rarity), source=item_id))
        for spec in self._as_rule_list(acquire.get("tagged_ingredient_choices")):
            for _ in range(int(spec.get("count", 1))):
                self.s.pending.append(self.make_choice("ingredient", source=item_id, tag_filter=spec.get("tag")))
        bundle = acquire.get("bundle_choice")
        if isinstance(bundle, dict):
            options = bundle.get("options", {})
            if isinstance(options, list):
                options = {str(option["id"]): option for option in options if isinstance(option, dict) and option.get("id")}
            if isinstance(options, dict) and options:
                self.s.pending.append(
                    PendingChoice(
                        kind="bundle",
                        offers=[str(option_id) for option_id in options],
                        can_skip=False,
                        source=item_id,
                        details={"options": options},
                    )
                )

    def add_essence(self, essence_id: str) -> None:
        if essence_id in self.s.essences or essence_id in self.s.consumed_essences:
            return
        self.s.essences.append(essence_id)
        self._snapshot_essence_baseline(essence_id)
        self.s.last_log.append(f"获得精粹：{self.catalog.essences[essence_id]['name']}。")

    def _snapshot_essence_baseline(self, essence_id: str) -> None:
        """Start all supported trigger scopes at acquisition or repeat use."""
        counts = dict(self.s.stats.setdefault("event_counts", {}))
        values = dict(self.s.stats.setdefault("event_values", {}))
        # Round-scoped triggers must not count events that happened before the
        # essence was acquired. Persist this snapshot so the stateless agent
        # interface remains correct when a save is reloaded between actions.
        self.s.stats.setdefault("essence_baseline", {})[essence_id] = {
            "events": counts,
            "values": values,
            "round_events": dict(self._round_events),
            "spin": self.s.spin,
        }
        if "choice_event_count" in self.catalog.essences[essence_id].get("trigger", {}) and self.s.pending:
            current = self.s.pending[0]
            self._ensure_choice_identity(current)
            self.s.stats["essence_baseline"][essence_id].update(
                choice_uid=current.details["choice_uid"],
                choice_events=dict(current.details.get("event_counts", {})),
            )
        if "consecutive_choice_rounds" in self.catalog.essences[essence_id].get("trigger", {}):
            completed = self.s.stats.get("choice_round_streak", {}).get("last_spin") == self.s.spin
            self.s.stats["essence_baseline"][essence_id]["choice_round_start"] = max(1, self.s.spin + int(completed))

    def _ensure_choice_identity(self, choice: PendingChoice) -> None:
        """Lazily give legacy and new choices a saveable non-random identity."""
        if "choice_uid" not in choice.details:
            uid = int(self.s.stats.get("next_choice_uid", 1))
            self.s.stats["next_choice_uid"] = uid + 1
            choice.details["choice_uid"] = uid

    def _record_choice_resolution(self, kind: str, skipped: bool) -> None:
        """Count completed reward rounds, never multiple picks as rounds.

        The reward phase completes when the pending queue becomes empty.
        Any skipped reward breaks the streak; optional later choices in the
        same spin cannot count as another completed round.
        """
        if kind == "run_end":
            return
        progress = self.s.stats.get("choice_round_progress", {})
        if progress.get("spin") != self.s.spin:
            progress = {"spin": self.s.spin, "taken": 0, "skipped": False}
            self.s.stats["choice_round_progress"] = progress
        if skipped:
            progress["skipped"] = True
            self.s.stats["choice_round_streak"] = {"last_spin": self.s.spin, "length": 0, "start_spin": self.s.spin + 1}
        else:
            progress["taken"] = int(progress.get("taken", 0)) + 1
        if self.s.pending or progress.get("skipped") or not progress.get("taken"):
            return
        streak = self.s.stats.get("choice_round_streak", {})
        if streak.get("last_spin") == self.s.spin:
            return
        consecutive = streak.get("last_spin") == self.s.spin - 1
        length = int(streak.get("length", 0)) + 1 if consecutive else 1
        self.s.stats["choice_round_streak"] = {"last_spin": self.s.spin, "length": length, "start_spin": self.s.spin - length + 1}

    def emit(self, event: str, amount: int = 1, value: int = 0, *, source_index: int | None = None, source_tags: list[str] | None = None) -> None:
        self._round_events[event] += amount
        if value:
            self._round_event_values[event] += value
        totals = self.s.stats.setdefault("event_counts", {})
        totals[event] = int(totals.get(event, 0)) + amount
        values = self.s.stats.setdefault("event_values", {})
        values[event] = int(values.get(event, 0)) + value
        for item_id in list(self.s.items):
            item = self.catalog.items[item_id]
            bonus = item.get("event_bonus", {})
            if event in bonus:
                self._gain_gold(int(bonus[event]) * amount, item["name"])
                self._record_item_trigger(item_id, amount)
            every_bonus = item.get("event_bonus_every", {}).get(event)
            if every_bonus:
                every = max(1, int(every_bonus.get("every", 1)))
                bonus_amount = int(every_bonus.get("amount", 0))
                counters = self.s.stats.setdefault("item_event_counts", {})
                key = f"{item_id}:{event}"
                previous = int(counters.get(key, 0))
                current = previous + amount
                counters[key] = current
                crossed = (current // every) - (previous // every)
                if crossed and bonus_amount:
                    self._gain_gold(crossed * bonus_amount, item["name"])
                    self._record_item_trigger(item_id, crossed)
                choice_extra = int(every_bonus.get("choice_extra", 0))
                if crossed and choice_extra:
                    self.s.flags["ingredient_choice_extra"] = int(self.s.flags.get("ingredient_choice_extra", 0)) + crossed * choice_extra
                    self._record_item_trigger(item_id, crossed)
                if crossed:
                    for token, token_amount in every_bonus.get("tokens", {}).items():
                        self._gain_token(token, crossed * int(token_amount), item["name"])
                    if every_bonus.get("tokens"):
                        self._record_item_trigger(item_id, crossed)
            flag_bonus = item.get("event_flag_increment", {}).get(event)
            if flag_bonus:
                flag = str(flag_bonus["flag"])
                self.s.flags[flag] = int(self.s.flags.get(flag, 0)) + int(flag_bonus.get("amount", 1)) * amount
                self._record_item_trigger(item_id, amount)
            item_reward = item.get("event_item_reward", {}).get(event)
            if item_reward:
                awarded = 0
                for _ in range(int(item_reward.get("count", 1)) * amount):
                    reward_id = self._draw_available_item(int(item_reward["rarity"]))
                    if reward_id is None:
                        break
                    self.add_item(reward_id)
                    awarded += 1
                if awarded:
                    self._record_item_trigger(item_id, awarded)
            script = item.get("script")
            round_key = f"item:{item_id}:{event}"
            if script == "reaction_window" and event in {"removed", "generated", "transformed"} and not self._round_events.get(round_key):
                self._round_events[round_key] = 1
                self._gain_gold(3, item["name"])
            elif script == "alchemy_insurance" and event == "removed" and not self._round_events.get(round_key):
                self._round_events[round_key] = 1
                self._gain_gold(4, item["name"])
                self.emit("insurance")
            elif script == "counter_calibrator" and event == "periodic" and not self._round_events.get(round_key):
                self._round_events[round_key] = 1
                self._gain_gold(3, item["name"])
            elif script == "experiment_notebook" and event == "permanent_bonus" and self._round_events.get(round_key, 0) < 3:
                self._round_events[round_key] += 1
                self._gain_gold(1, item["name"])
            elif script == "cat_observation_log" and event == "cat_bonus" and not self._round_events.get(round_key):
                self._round_events[round_key] = 1
                self._gain_gold(4, item["name"])
        if source_index is not None:
            self._adjacent_event_growth(event, source_index, amount, source_tags)
        if event == "generated":
            self._check_immediate_essences(event)

    def _adjacent_event_growth(self, event: str, source_index: int, count: int = 1, source_tags: list[str] | None = None) -> None:
        """Resolve data-declared growth at the actual adjacent event once."""
        if count <= 0 or not 0 <= source_index < len(self._board):
            return
        tags = set(source_tags if source_tags is not None else self.catalog.ingredients[self._board[source_index].def_id].get("tags", []))
        for index in self._neighbors(source_index):
            listener = self._board[index]
            if not self._present(listener):
                continue
            rule = self.catalog.ingredients[listener.def_id].get("adjacent_event_growth", {})
            if rule.get("event") != event or not set(rule.get("source_tags_all", [])).issubset(tags):
                continue
            key = f"adjacent_event:{listener.uid}:{event}"
            if rule.get("once_per_round") and self._round_events.get(key):
                continue
            self._round_events[key] = 1
            self._permanent_bonus(listener, int(rule.get("amount", 1)) * count)

    def _emit_distinct_round_event(self, event: str, identity: str) -> None:
        """Emit an aggregate event only for the first identity this round."""
        key = f"distinct:{event}:{identity}"
        if self._round_events.get(key):
            return
        self._round_events[key] = 1
        self.emit(event)

    def _check_immediate_essences(self, event: str) -> None:
        """Resolve explicitly immediate event-count essences.

        Most essences retain the engine's normal end-of-action check.  The
        ``immediate`` flag is a generic opt-in for effects (such as the ban
        essence) that must take effect before a later generator in the same
        action can run.
        """
        for essence_id in list(self.s.essences):
            data = self.catalog.essences[essence_id]
            trigger = data.get("trigger", {})
            if not trigger.get("immediate"):
                continue
            event_spec = trigger.get("event_count", {})
            if event_spec.get("event") != event:
                continue
            if self._trigger_ready(essence_id, trigger):
                self._trigger_essence(essence_id, data)

    def _item_choice_rewards_for_event(self, event: str) -> list[PendingChoice]:
        """Build data-driven choice rewards for an event, without stacking duplicates."""
        rewards: list[PendingChoice] = []
        seen_items: set[str] = set()
        for item_id in list(self.s.items):
            if item_id in seen_items:
                continue
            seen_items.add(item_id)
            item = self.catalog.items.get(item_id, {})
            spec = item.get("event_choice_reward", {}).get(event)
            if not spec:
                continue
            choices = max(0, int(spec.get("choices", 1)))
            for _ in range(choices):
                rewards.append(
                    self.make_choice(
                        str(spec.get("kind", "ingredient")),
                        source=str(spec.get("source", item_id)),
                        can_skip=bool(spec.get("can_skip", True)),
                    )
                )
            if choices:
                self._record_item_trigger(item_id, choices)
        return rewards

    def _gain_gold(self, amount: int, source: str) -> None:
        if amount == 0:
            return
        previous = self.s.gold
        self.s.gold = max(0, self.s.gold + amount)
        actual = self.s.gold - previous
        if actual:
            self.s.last_log.append(f"{source}：{actual:+d}g。")

    def _record_item_trigger(self, item_id: str, amount: int = 1) -> None:
        if amount <= 0:
            return
        counters = self.s.stats.setdefault("item_trigger_counts", {})
        counters[item_id] = int(counters.get(item_id, 0)) + int(amount)

    def _draw_available_item(self, rarity: int | None) -> str | None:
        """Draw a legal auto-granted item, or omit an exhausted reward.

        Explicit tiers never substitute other tiers. Unrestricted rewards
        retain the existing rarity roll and fallback order while stocked;
        an empty pool consumes no draw and cannot interrupt other effects.
        """
        if rarity is None:
            if not any(self._defs_at_rarity("item", tier) for tier in range(1, 5)):
                return None
            return self._draw_definition("item", self.roll_rarity("item"))
        rows = self._defs_at_rarity("item", rarity)
        if not rows:
            return None
        return self.r.weighted_choice([(row["id"], float(row.get("pool_weight", 1.0))) for row in rows])

    def _has_tag(self, instance: IngredientInstance, tag: str) -> bool:
        return tag in self.catalog.ingredients[instance.def_id].get("tags", [])

    def _neighbors(self, index: int) -> list[int]:
        if self._all_adjacent:
            return [i for i in range(len(self._board)) if i != index]
        # Existing normal-board semantics remain unchanged (the project has
        # historically used eight-neighbour adjacency).  Fun boards opt into
        # the explicitly requested orthogonal topology.
        if self.s.fun_mode in {"giant", "minimal"}:
            regular = set(orthogonal_indices(self._coords, index))
        else:
            regular = set(adjacent_indices(self._coords, index))
        if self._panorama:
            corners = {i for i, coord in enumerate(self._coords) if self._is_corner(coord)}
            if index in corners:
                return [i for i in range(len(self._board)) if i != index]
            regular.update(corners)
            regular.discard(index)
        return sorted(regular)

    def _board_bounds(self) -> tuple[int, int]:
        # Derive the complete board's bounds, not the sampled/occupied cells.
        # An expansion cell is an edge but does not change the four corners.
        coords = board_coords(False, self.s.fun_mode)
        return max(row for row, _ in coords), max(col for _, col in coords)

    def _is_edge(self, coord: tuple[int, int]) -> bool:
        max_row, max_col = self._board_bounds()
        return is_edge(coord, max_row=max_row, max_col=max_col)

    def _is_corner(self, coord: tuple[int, int]) -> bool:
        max_row, max_col = self._board_bounds()
        return is_corner(coord, max_row=max_row, max_col=max_col)

    def _present(self, instance: IngredientInstance) -> bool:
        return any(x.uid == instance.uid for x in self.s.ingredients)

    def _item_bonus(self, definition: dict[str, Any]) -> int:
        total = 0
        tags = set(definition.get("tags", []))
        for item_id in self.s.items:
            for bonus in self.catalog.items[item_id].get("bonuses", []):
                if bonus.get("rarity") and int(bonus["rarity"]) != int(definition.get("rarity", 0)):
                    continue
                matches = False
                if bonus.get("id") == definition["id"]:
                    matches = True
                if definition["id"] in bonus.get("ids", []):
                    matches = True
                if bonus.get("tag") in tags:
                    matches = True
                if tags.intersection(bonus.get("tags", [])):
                    matches = True
                if "base" in bonus and int(bonus["base"]) == int(definition.get("base", 0)):
                    matches = True
                if matches:
                    total += int(bonus.get("amount", 0))
        return total

    def _conditional_item_bonus(self, index: int, definition: dict[str, Any]) -> int:
        """Evaluate data-driven value bonuses that depend on board context."""
        total = 0
        for item_id in self.s.items:
            for rule in self.catalog.items[item_id].get("conditional_bonuses", []):
                if not isinstance(rule, dict):
                    continue
                if rule.get("tag") and rule["tag"] not in definition.get("tags", []):
                    continue
                if rule.get("id") and rule["id"] != definition.get("id"):
                    continue
                condition = rule.get("condition", {})
                if condition.get("adjacent_same_field"):
                    field = str(condition["adjacent_same_field"])
                    value = definition.get(field)
                    if value is None or not any(
                        self.catalog.ingredients[self._board[n].def_id].get(field) == value
                        for n in self._neighbors(index)
                        if n < len(self._board) and self._present(self._board[n])
                    ):
                        continue
                total += int(rule.get("amount", 0))
        return total

    def stable_ingredient_value(self, instance: IngredientInstance) -> int:
        """Return value layers independent of position, random rolls and auras.

        This read-only view is shared by the board calculation and UI. It does
        not consume RNG or emit events, so inspecting an off-board ingredient
        cannot change the next spin. Board-dependent bonuses still belong to
        the normal settlement pipeline.
        """
        definition = self.catalog.ingredients[instance.def_id]
        global_bonus = int(self.s.flags.get("global_permanent_bonuses", {}).get(definition["id"], 0))
        normal_minimal = self.s.fun_mode == "minimal" and "special" not in definition.get("tags", [])
        if normal_minimal:
            global_bonus *= 2
        value = int(definition.get("base", 0)) + instance.permanent_bonus + global_bonus + self._item_bonus(definition)
        rarity = int(definition.get("rarity", 0))
        if normal_minimal and 1 <= rarity <= 4:
            value += rarity
        if self.s.flags.get("ingredient_generation_permanently_disabled") and self.ingredient_has_generation(definition):
            generation_bonus = int(self.s.flags.get("ingredient_generation_bonus", 0))
            applied_bonus = int(instance.flags.get("ingredient_generation_bonus_applied", 0))
            unapplied = max(0, generation_bonus - applied_bonus)
            if self.s.fun_mode == "minimal":
                unapplied *= 2
            value += unapplied
        override = self.difficulty_ingredient_override(instance.def_id)
        return int(override.get("force_value", value))

    def _base_values(self) -> list[int]:
        values: list[int] = []
        for i, instance in enumerate(self._board):
            definition = self.catalog.ingredients[instance.def_id]
            difficulty_override = self.difficulty_ingredient_override(instance.def_id)
            value = self.stable_ingredient_value(instance) + self._conditional_item_bonus(i, definition)
            spec = definition.get("value", {})
            neighbors = [self._board[n] for n in self._neighbors(i)]
            tags = set(definition.get("tags", []))
            if spec.get("if_adjacent_tag") and any(self._has_tag(x, spec["if_adjacent_tag"]) for x in neighbors):
                value += int(spec.get("bonus", 0)); self.emit("adjacency")
            if spec.get("if_adjacent_ids") and any(x.def_id in spec["if_adjacent_ids"] for x in neighbors):
                value += int(spec.get("bonus", 0)); self.emit("adjacency")
            if spec.get("if_no_adjacent_tag") and not any(self._has_tag(x, spec["if_no_adjacent_tag"]) for x in neighbors):
                value += int(spec.get("bonus", 0))
            if spec.get("position") == "edge" and self._is_edge(self._coords[i]):
                value += int(spec.get("bonus", 0))
            if spec.get("position") == "corner" and self._is_corner(self._coords[i]):
                value += int(spec.get("bonus", 0))
            if spec.get("count_id"):
                value += sum(1 for x in self._board if x.def_id == spec["count_id"]) * int(spec.get("per", 1))
            if spec.get("count_adjacent_tag"):
                value += sum(1 for x in neighbors if self._has_tag(x, spec["count_adjacent_tag"])) * int(spec.get("per", 1))
                self.emit("adjacency")
            if "random_min" in spec:
                value += self.r.randint(int(spec["random_min"]), int(spec["random_max"]))
                self.emit("chance_checked")
            if "coin_flip" in spec:
                forced = bool(self.s.flags.pop("coin_force_success", False))
                success = forced or self._chance(0.5)
                value += int(spec["coin_flip"]) if success else 0
                instance.flags["coin_success"] = success
                if not success and any(x.def_id == "lucky_coin" for x in self.s.ingredients):
                    self.s.flags["coin_force_success"] = True
            if spec.get("chance_zero") and not self.negative_disabled() and self._chance(float(spec["chance_zero"]), negative=True, source_index=i):
                value = 0
            if "force_value" in difficulty_override:
                value = int(difficulty_override["force_value"])
            values.append(value)
            if "cat" in tags and value > int(definition.get("base", 0)):
                self.emit("cat_bonus")
        return values

    def _apply_multipliers(self, values: list[int]) -> list[int]:
        result = [float(x) for x in values]
        for i, source in enumerate(self._board):
            definition = self.catalog.ingredients[source.def_id]
            aura = definition.get("aura")
            if aura:
                for n in self._neighbors(i):
                    target = self.catalog.ingredients[self._board[n].def_id]
                    target_tags = set(target.get("tags", []))
                    matches = aura.get("id") == target["id"] or target["id"] in aura.get("ids", [])
                    matches = matches or aura.get("tag") in target_tags or bool(target_tags.intersection(aura.get("tags", [])))
                    if matches:
                        result[n] *= float(aura["multiplier"]); self.emit("adjacency")
        for i, source in enumerate(self._board):
            definition = self.catalog.ingredients[source.def_id]
            if definition.get("script") == "double_potion":
                for n in self._neighbors(i):
                    result[n] *= 2 * self._potion_effect_multiplier(); self.emit("adjacency")
            if definition.get("script") == "focus_lens":
                neighbors = self._neighbors(i)
                if neighbors:
                    result[self.r.choice(neighbors)] *= 2; self.emit("adjacency")
        directions = [(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]
        coord_to_index = {coord: i for i, coord in enumerate(self._coords)}
        for i, source in enumerate(self._board):
            definition = self.catalog.ingredients[source.def_id]
            if definition.get("prism"):
                times = 1 + sum(int(self.catalog.items[x].get("extra_prism_directions", 0)) for x in self.s.items)
                for _ in range(times):
                    dr, dc = self.r.choice(directions)
                    row, col = self._coords[i]
                    affected = 0
                    max_row, max_col = self._board_bounds()
                    for step in range(1, max(max_row, max_col) + 2):
                        target = coord_to_index.get((row + dr * step, col + dc * step))
                        if target is not None:
                            multiplier = float(definition["prism"])
                            if self.s.flags.get("prism_multiplier_spins", 0):
                                multiplier *= float(self.s.flags.get("prism_multiplier", 1.0))
                            result[target] *= multiplier; affected += 1
                    if affected:
                        self._emit_distinct_round_event("prism_type", definition["id"])
                        self.emit("adjacency", affected)
        for i, source in enumerate(self._board):
            definition = self.catalog.ingredients[source.def_id]
            script = self.catalog.ingredients[source.def_id].get("script")
            if script == "collector":
                result[i] += 2 * len({int(self.catalog.ingredients[x.def_id].get("rarity", 0)) for x in self._board})
            elif script == "mimic_spirit":
                neigh = self._neighbors(i)
                if neigh:
                    result[i] += max(result[n] for n in neigh)
            elif script == "mirror":
                row, col = self._coords[i]
                max_row, max_col = self._board_bounds()
                opposite = (max_row - row, max_col - col)
                if opposite in coord_to_index:
                    result[i] += result[coord_to_index[opposite]]
            elif script == "pigment":
                pigment_color = definition.get("pigment_color")
                same_color = pigment_color is not None and any(
                    self.catalog.ingredients[self._board[n].def_id].get("pigment_color") == pigment_color
                    for n in self._neighbors(i)
                    if n < len(self._board) and self._present(self._board[n])
                )
                other_colors = {
                    self.catalog.ingredients[self._board[n].def_id].get("pigment_color")
                    for n in self._neighbors(i)
                    if n < len(self._board)
                    and self._present(self._board[n])
                    and self.catalog.ingredients[self._board[n].def_id].get("pigment_color") is not None
                    and self.catalog.ingredients[self._board[n].def_id].get("pigment_color") != pigment_color
                }
                if same_color:
                    result[i] += 1; self.emit("adjacency")
                if other_colors:
                    result[i] += 2; self.emit("adjacency")
                colors_on_board = {
                    self.catalog.ingredients[x.def_id].get("pigment_color")
                    for x in self._board
                    if self._present(x) and self.catalog.ingredients[x.def_id].get("pigment_color") is not None
                }
                required_colors = set(self.catalog.progression.get("pigment_required_colors", []))
                if required_colors and required_colors.issubset(colors_on_board):
                    result[i] += 2
        flag_multipliers = [
            ("cat_multiplier_spins", "cat", self.s.flags.get("cat_multiplier", 1)),
            ("liquid_multiplier_spins", "liquid", self.s.flags.get("liquid_multiplier", 1)),
            ("glass_multiplier_spins", "glass", self.s.flags.get("glass_multiplier", 1)),
            ("mineral_multiplier_spins", "mineral", self.s.flags.get("mineral_multiplier", 1)),
        ]
        for flag, tag, multiplier in flag_multipliers:
            if self.s.flags.get(flag, 0):
                for i, inst in enumerate(self._board):
                    tags = set(self.catalog.ingredients[inst.def_id].get("tags", []))
                    if tag == "mineral" and tags.intersection({"stone","ore","metal"}) or tag in tags:
                        result[i] *= float(multiplier)
        if self.s.flags.get("global_multiplier_spins", 0):
            result = [x * float(self.s.flags.get("global_multiplier", 1)) for x in result]
        return [int(x) for x in result]

    def _chance(self, chance: float, *, negative: bool = False, source_index: int | None = None) -> bool:
        bonus = sum(float(self.catalog.items[x].get("chance_bonus", 0)) for x in self.s.items)
        if negative:
            bonus += sum(float(self.catalog.ingredients[x.def_id].get("global_modifier", {}).get("negative_chance", 0)) for x in self._board)
            cancel = sum(float(self.catalog.items[x].get("negative_cancel_chance", 0)) for x in self.s.items)
            if cancel and self.r.random() < min(1.0, cancel):
                for item_id in self.s.items:
                    if self.catalog.items[item_id].get("negative_cancel_chance"):
                        self._record_item_trigger(item_id)
                self.emit("negative_prevented")
                return False
        self.emit("chance_checked")
        if self.s.flags.pop("guarantee_chance_next", False):
            success = True
        else:
            success = self.r.random() < min(1.0, max(0.0, chance + bonus))
        if negative and success:
            self.emit("negative_triggered", source_index=source_index)
        return success

    def _require_action_window(self, *, allow_pending: bool = False) -> None:
        """Enforce the same action window advertised to every client."""
        if self.s.status != "playing":
            raise GameError("本局已经结束")
        if self.s.pending and not allow_pending:
            raise GameError("请先处理当前选择")

    def spin(self) -> int:
        self._require_action_window()
        # Repeater memory belongs to this settlement, not the previous turn.
        # Clear only after action validation so rejected spins remain read-only.
        self.s.stats.pop("last_potion", None)
        extra_choice_active = int(self.s.flags.get("extra_choice_spins", 0)) > 0
        self.s.last_log = []
        gold_at_spin_start = self.s.gold
        endless_at_spin_start = bool(self.s.endless_mode)
        self._round_events = defaultdict(int)
        self._round_event_values = defaultdict(int)
        self.s.stats["round_events"] = {}
        self.s.stats["round_event_values"] = {}
        self.s.stats["round_removed_values"] = []
        self._removed_values = []
        self.s.spin += 1
        self.s.spins_left -= 1
        self._coords = board_coords(self.s.expanded, self.s.fun_mode)
        self._board = self.r.sample(self.s.ingredients, min(len(self.s.ingredients), len(self._coords)))
        self._coords = self._coords[:len(self._board)]
        for inst in self._board:
            inst.age += 1
            inst.counter += 1
        self._trigger_context_essences("board_tag_appearance", instances=list(self._board))
        self._panorama = "panorama_mirror" in self.s.items and self.s.spin % 3 == 0
        if self._panorama:
            self.emit("panorama")
        self._all_adjacent = bool(
            self.s.flags.get("all_adjacent_spins", 0)
            or ("global_reaction_field" in self.s.items and self.s.spin % 3 == 0)
        )
        self._trigger_pigment_pair_essences()
        base_values = self._base_values()
        self._values = self._apply_multipliers(base_values)
        component_income = sum(self._values)
        income = component_income
        if "anomaly_recorder" in self.s.items:
            if any(value >= int(self.catalog.ingredients[inst.def_id].get("base", 0)) * 3 and value > 0 for value, inst in zip(self._values, self._board)):
                self._gain_gold(5, "异常记录仪"); self.emit("anomaly")
        open_all_chests = bool(self.s.flags.pop("open_all_chests_next", False))
        if "master_key" in self.s.items or open_all_chests:
            force_open = open_all_chests
            for i, inst in list(enumerate(self._board)):
                if self._present(inst) and self._has_tag(inst, "chest") and (force_open or self._chance(0.3)):
                    self._remove(inst, "opened", i)
        for item_id in list(self.s.items):
            item = self.catalog.items[item_id]
            income += int(item.get("per_spin_gold", 0))
            if item.get("script") == "reagent_rack":
                reagent_count = sum(1 for x in self.s.items if x.endswith("_reagent"))
                income += 2 * reagent_count
                if reagent_count:
                    self._record_item_trigger(item_id)
            if item.get("script") == "impossible_container":
                cap = int(item.get("per_spin_cap", 20))
                container_bonus = min(cap, max(0, len(self.s.ingredients) - 30))
                income += container_bonus
                if container_bonus:
                    self._record_item_trigger(item_id)
            if item.get("script") == "automatic_stirrer" and self.s.spin % 10 == 0:
                liquids = [x for x in self._board if self._present(x) and self._has_tag(x, "liquid")]
                if liquids: self._permanent_bonus(self.r.choice(liquids), 1)
            if item.get("script") == "goggles" and self.s.tokens.get("remove", 0) >= 3:
                self._gain_token("remove", 1, "护目镜")
            periodic = item.get("periodic_token")
            if periodic and self.s.spin % int(periodic["every"]) == 0:
                self._gain_token(periodic["token"], int(periodic["amount"]), item["name"])
                if periodic["token"] == "essence": self.emit("distilled_essence")
            periodic_choice = item.get("periodic_choice")
            if periodic_choice and self.s.spin % int(periodic_choice["every"]) == 0:
                for _ in range(int(periodic_choice.get("amount", 1))):
                    self.s.pending.append(self.make_choice(periodic_choice["kind"], source=item_id))
            choice_bonus = item.get("periodic_choice_bonus")
            if choice_bonus and self.s.spin % int(choice_bonus["every"]) == 0:
                self.s.flags["credit_card_bonus"] = int(choice_bonus["bonus"])
        income_multiplier = 1
        if self.s.flags.pop("double_next_income", False):
            income_multiplier = 2
            income *= 2
            self.emit("ledger_used")
            if "double_ledger" in self.s.items:
                self._record_item_trigger("double_ledger")
        self._run_active_effects()
        self._run_item_round_effects()
        # Some active effects explicitly replace this round's settled slot
        # value (removal magic, a failed gambler). Their changes must reach
        # the payout as well as last_board, while permanent growth/removal
        # rewards retain the established timing and item income is unchanged.
        income += (sum(self._values) - component_income) * income_multiplier
        self._run_round_conditions(income)
        if "coin_jar" in self.s.items and income <= 20:
            income += 2
            self.s.last_log.append("零钱罐：+2g。")
        if self.s.flags.get("order_book_sacrifice"):
            income = 0
            self.s.flags.pop("order_book_sacrifice", None)
            self.s.flags["order_book_reward"] = True
            self.emit("order_book_used")
        self.s.gold = max(0, self.s.gold + income)
        # Post-settlement entertainment hooks run before ordinary ingredient
        # rewards are queued.  They use the same remove/transform paths as
        # normal card effects, so on-remove listeners remain consistent.
        self._process_mutations()
        self._rapid_delete_one()
        self.s.stats["last_income"] = income
        self._check_peace_goal()
        round_gold = max(0, int(self.s.gold - gold_at_spin_start))
        self.s.stats["last_round_gold"] = round_gold
        self.s.stats["highest_single_turn_gold"] = max(
            int(self.s.stats.get("highest_single_turn_gold", 0)), round_gold
        )
        if endless_at_spin_start:
            self.s.stats["highest_endless_single_turn_gold"] = max(
                int(self.s.stats.get("highest_endless_single_turn_gold", 0)), round_gold
            )
        self.s.last_log.insert(0, f"第{self.s.spin}回合：成分与道具合计 {income:+d}g。")
        self.s.last_board = [
            {
                "slot": i + 1,
                "coord": list(self._coords[i]),
                "uid": inst.uid,
                "id": inst.def_id,
                "name": self.catalog.ingredients[inst.def_id]["name"],
                "value": self._values[i],
                # Keep removed board instances observable without making them
                # look like active ingredients. Essence board predicates use
                # the same presence rule.
                "present": self._present(inst),
            }
            for i, inst in enumerate(self._board)
        ]
        self.s.stats["last_board_topology"] = {
            "all_adjacent": self._all_adjacent,
            "panorama": self._panorama,
        }
        self._decay_flags()
        interval = self.slag_interval(self.s.difficulty, self.catalog.progression)
        if interval and (self.s.endless_mode or self.s.order_index < 11) and self.s.spin % interval == 0:
            self.add_ingredient("slag")
            self.s.last_log.append("难度规则向成分池加入1个废渣。")
        if self.s.status == "playing":
            force_choose = bool(self.s.flags.pop("force_choose", False))
            if self.s.spins_left > 0 or self.s.fun_mode == "rapid":
                normal_rewards = [
                    self.make_choice("ingredient", source="spin", can_skip=not force_choose)
                ]
                if self.s.fun_mode == "rapid":
                    normal_rewards.append(
                        self.make_choice("ingredient", source="spin", can_skip=not force_choose)
                    )
                # Keep the first normal reward ahead of the second, while
                # preserving any pre-existing periodic choices behind them.
                for reward in reversed(normal_rewards):
                    self.s.pending.insert(0, reward)
            if extra_choice_active:
                self.s.pending.append(self.make_choice("ingredient", source="essence"))
            if self.s.spins_left <= 0:
                self._settle_order()
        if extra_choice_active:
            self.s.flags["extra_choice_spins"] = max(0, int(self.s.flags.get("extra_choice_spins", 0)) - 1)
        self.check_essences()
        self._sync_rng()
        return income

    def _decay_flags(self) -> None:
        for key in ["all_adjacent_spins","cat_multiplier_spins","liquid_multiplier_spins","glass_multiplier_spins","mineral_multiplier_spins","global_multiplier_spins","prism_multiplier_spins"]:
            if int(self.s.flags.get(key, 0)) > 0:
                self.s.flags[key] = int(self.s.flags[key]) - 1

    def _run_active_effects(self) -> None:
        force_periodic = bool(self.s.flags.pop("force_periodic_next", False))
        for i, inst in list(enumerate(self._board)):
            if not self._present(inst):
                continue
            definition = self.catalog.ingredients[inst.def_id]
            periodic = definition.get("periodic_gold")
            if periodic:
                every = self._effective_period(int(periodic["every"]))
                if force_periodic or inst.counter >= every:
                    self._gain_gold(int(periodic["gold"]), definition["name"])
                    self._periodic_reset(inst, every); self.emit("periodic")
            periodic_spawn = definition.get("periodic_spawn")
            if periodic_spawn:
                every = self._effective_period(int(periodic_spawn["every"]), spawning=True)
                if force_periodic or inst.counter >= every:
                    if not self.ingredient_generation_disabled():
                        self._spawn_random(
                            tag=periodic_spawn.get("tag"),
                            source=i,
                            minimum_rarity=int(periodic_spawn.get("minimum_rarity", 1)),
                            minimum_rarity_chance=periodic_spawn.get("minimum_rarity_chance"),
                            origin="ingredient",
                        )
                    if periodic_spawn.get("permanent_bonus"):
                        self._permanent_bonus(inst, int(periodic_spawn["permanent_bonus"]))
                    self._periodic_reset(inst, every); self.emit("periodic")
            if definition.get("spawn_each_spin") and not self.ingredient_generation_disabled():
                spec = definition["spawn_each_spin"]
                self._spawn_random(tag=spec.get("tag"), def_id=spec.get("id"), source=i, origin="ingredient")
            if definition.get("chance_spawn") and not self.ingredient_generation_disabled():
                spec = definition["chance_spawn"]
                chance = float(spec["chance"]) + sum(float(self.catalog.items[x].get("spawn_chance_bonus", 0)) for x in self.s.items)
                if self._chance(chance):
                    guarantee = spec.get("success_guarantee", {})
                    counter_name = guarantee.get("counter")
                    counters = self.s.stats.setdefault("spawn_counters", {})
                    previous_successes = int(counters.get(counter_name, 0)) if counter_name else 0
                    guarantee_active = bool(
                        counter_name
                        and previous_successes >= int(guarantee.get("every", 0))
                    )
                    minimum_rarity = int(guarantee.get("minimum_rarity", 1)) if guarantee_active else 1
                    created = self._spawn_random(
                        tag=spec.get("tag"),
                        rarity=spec.get("rarity"),
                        minimum_rarity=minimum_rarity,
                        def_id=spec.get("id"),
                        source=i,
                        origin="ingredient",
                    )
                    if created and counter_name:
                        counters[counter_name] = 0 if guarantee_active else previous_successes + 1
            if definition.get("periodic_jackpot") and self._chance(float(definition["periodic_jackpot"]["chance"])):
                self._gain_gold(int(definition["periodic_jackpot"]["gold"]), definition["name"])
            if definition.get("stash"):
                amount = int(definition["stash"]) + int(self.s.flags.get("monster_stash_bonus", 0))
                inst.stored_gold += amount
                self.emit("stored", value=amount)
                if "storage_log" in self.s.items:
                    used = self._round_event_values.get("storage_log_bonus", 0)
                    if used < 3:
                        bonus = min(1, 3 - used); self._gain_gold(bonus, "储藏记录"); self._round_event_values["storage_log_bonus"] += bonus
            potion = definition.get("potion")
            if potion:
                self._trigger_potion(i, inst, potion)
                continue
            self._run_script(i, inst, definition.get("script"), force_periodic=force_periodic)
            if not self._present(inst):
                continue
            # A legacy definition may provide one chance_transform mapping.
            # Newer definitions can provide mutually-exclusive weighted
            # chance_transforms, allowing one instance to have several
            # alternative outcomes without performing multiple transforms in
            # the same round.
            transform = definition.get("chance_transform")
            transforms = definition.get("chance_transforms")
            if transforms is not None:
                options = [item for item in transforms if isinstance(item, dict)]
                total_chance = sum(max(0.0, float(item.get("chance", 0.0))) for item in options)
                if options and total_chance > 0 and self._chance(total_chance):
                    pick = self.r.random() * total_chance
                    cumulative = 0.0
                    for option in options:
                        cumulative += max(0.0, float(option.get("chance", 0.0)))
                        if pick < cumulative:
                            self._transform(inst, option["into"])
                            break
            elif transform and self._chance(float(transform["chance"])):
                self._transform(inst, transform["into"])
            transform_after = definition.get("transform_after")
            if transform_after and inst.age >= int(transform_after["spins"]):
                if not self._prevent_countdown(i, inst):
                    self._transform(inst, transform_after["into"])
            if definition.get("remove_after"):
                remove_after = int(definition["remove_after"]) + int(
                    self.difficulty_ingredient_override(inst.def_id).get("remove_after_bonus", 0)
                )
            else:
                remove_after = 0
            if remove_after and inst.age >= remove_after:
                if not self._prevent_countdown(i, inst):
                    self._remove(inst, "expired", i)

    def _run_item_round_effects(self) -> None:
        """Apply declarative item effects that inspect the settled board.

        Each target is checked independently and only while it is still in the
        pool, so earlier removals in the same pass naturally prevent a second
        check of an already departed instance.
        """
        for item_id in list(self.s.items):
            effect = self.catalog.items[item_id].get("round_effect", {})
            remove_rule = effect.get("remove_tag_chance")
            if not remove_rule:
                continue
            tag = remove_rule.get("tag")
            chance = float(remove_rule.get("chance", 0.0))
            reason = str(remove_rule.get("reason", "removed"))
            for board_index, target in list(enumerate(self._board)):
                if not self._present(target) or not self._has_tag(target, tag):
                    continue
                if self.r.random() < chance:
                    self._remove(target, reason, board_index)

    def _mutation_eligible(self, instance: IngredientInstance) -> bool:
        definition = self.catalog.ingredients.get(instance.def_id, {})
        rarity = int(definition.get("rarity", 0))
        return (
            1 <= rarity <= 4
            and "special" not in definition.get("tags", [])
            and definition.get("offerable", True)
        )

    def _mutate_instance(self, instance: IngredientInstance) -> bool:
        """Mutate one existing instance without using the gain pipeline."""
        if not self._mutation_eligible(instance):
            return False
        old_id = instance.def_id
        old_rarity = int(self.catalog.ingredients[old_id].get("rarity", 1))
        # The 1% roll is an additional RNG draw only in mutation mode.  At
        # rarity 4 it intentionally stays in the rarity-4 pool.
        upgrade_hit = self.r.random() < 0.01
        upgraded = old_rarity < 4 and upgrade_hit
        target_rarity = min(4, old_rarity + (1 if upgraded else 0))
        candidates = self._defs_at_rarity("ingredient", target_rarity, exclude={old_id})
        if not candidates:
            candidates = self._defs_at_rarity("ingredient", target_rarity)
        if not candidates:
            # A custom/minimal catalog can legitimately contain only one
            # legal definition at this rarity.  The fifth draw still mutates
            # (and resets its counter); retaining the identity is the only
            # legal fallback when no alternative exists.
            candidates = [{"id": old_id, "pool_weight": 1.0}]
        new_id = self.r.weighted_choice(
            [(row["id"], float(row.get("pool_weight", 1.0))) for row in candidates]
        )
        # Materialize any legacy global growth before identity changes, then
        # preserve its attribution with the inherited permanent value. This
        # metadata is not an old card's mechanism counter.
        self._apply_generation_bonus_to_instance(instance)
        applied_generation_bonus = int(instance.flags.get("ingredient_generation_bonus_applied", 0))
        instance.def_id = new_id
        instance.age = 0
        instance.counter = 0
        instance.stored_gold = 0
        instance.flags = {"ingredient_generation_bonus_applied": applied_generation_bonus} if applied_generation_bonus else {}
        # A global generation-ban bonus still applies to a generator after it
        # transforms, but this is not a gain event and does not copy any old
        # mechanism state.
        self._apply_generation_bonus_to_instance(instance)
        self.emit("mutated")
        old_name = self.catalog.ingredients[old_id]["name"]
        new_name = self.catalog.ingredients[new_id]["name"]
        if upgraded and old_rarity < 4:
            marker = "【升级变异】"
        elif upgrade_hit:
            marker = "【最高稀有度】"
        else:
            marker = ""
        self.s.last_log.append(
            f"{old_name}（{old_rarity}级）→ {new_name}（{target_rarity}级）{marker}"
        )
        return True

    def _process_mutations(self) -> None:
        if self.s.fun_mode != "mutation" or self.s.spin <= 0 or self.s.spin % 5:
            return
        self.s.last_log.append(f"第{self.s.spin}次全局变异开始。")
        # The whole persisted pool participates, not only this spin's board.
        # Snapshot the instances so each one mutates at most once even if its
        # new identity happens to have mutation-capable mechanics.
        for instance in list(self.s.ingredients):
            if self._mutation_eligible(instance):
                self._mutate_instance(instance)

    def _rapid_delete_one(self) -> bool:
        """Remove one random legal ingredient through the normal pipeline."""
        if self.s.fun_mode != "rapid":
            return False
        candidates = [
            instance for instance in self.s.ingredients
            if self.catalog.ingredients[instance.def_id].get("removable", True)
        ]
        if not candidates:
            return False
        target = self.r.choice(candidates)
        board_index = next(
            (index for index, instance in enumerate(self._board)
             if instance.uid == target.uid and self._present(instance)),
            None,
        )
        removed = self._remove(target, "rapid", board_index)
        if removed:
            self.emit("rapid_removed")
        return removed

    def _effective_period(self, every: int, spawning: bool = False) -> int:
        reduction = sum(int(self.catalog.items[x].get("counter_reduction", 0)) for x in self.s.items)
        if spawning:
            reduction += sum(int(self.catalog.items[x].get("spawn_period_reduction", 0)) for x in self.s.items)
        return max(2, every - reduction)

    def _periodic_reset(self, inst: IngredientInstance, every: int) -> None:
        inst.counter = 0
        if "time_rift" in self.s.items and self._chance(float(self.catalog.items["time_rift"]["time_rift_chance"])):
            inst.counter = every - 1

    def _run_script(self, index: int, inst: IngredientInstance, script: str | None, *, force_periodic: bool = False) -> None:
        definition = self.catalog.ingredients.get(inst.def_id, {})
        neighbors = [n for n in self._neighbors(index) if n < len(self._board) and self._present(self._board[n])]
        self_growth = definition.get("periodic_permanent_bonus")
        if self_growth:
            every = self._effective_period(int(self_growth.get("every", 1)))
            if force_periodic or inst.counter >= every:
                self._permanent_bonus(inst, int(self_growth.get("amount", 1)))
                self._periodic_reset(inst, every)
                self.emit("periodic")
        adjacent_growth = definition.get("periodic_adjacent_permanent_growth")
        if adjacent_growth:
            every = self._effective_period(int(adjacent_growth.get("every", 1)))
            if force_periodic or inst.counter >= every:
                targets = [n for n in neighbors if not adjacent_growth.get("tag") or self._has_tag(self._board[n], str(adjacent_growth["tag"]))]
                count = min(len(targets), max(0, int(adjacent_growth.get("targets", 1))))
                if count == 1 and adjacent_growth.get("selection") == "choice":
                    selected = [self.r.choice(targets)]
                else:
                    selected = self.r.sample(targets, count)
                for target_index in selected:
                    self._permanent_bonus(self._board[target_index], int(adjacent_growth.get("amount", 1)))
                self._periodic_reset(inst, every)
                self.emit("periodic")
        board_gold = definition.get("board_presence_gold")
        if board_gold:
            present_ids = {x.def_id for x in self._board if self._present(x)}
            if set(board_gold.get("ids", [])).issubset(present_ids):
                self._gain_gold(int(board_gold.get("gold", 0)), definition["name"])
        if not script:
            return
        if script == "kitten": self._consume_first(index, {"milk"}, 9)
        elif script == "crow":
            for target_index in list(neighbors):
                target = self._board[target_index]
                if target.def_id == "coin" and self._remove(target, "consumed", target_index):
                    self._gain_gold(9, definition["name"])
        elif script == "key": self._consume_first(index, tags={"chest"}, opened=True)
        elif script == "alcohol_lamp":
            rewards = definition.get("consume_rewards", {})
            alcohol_reward = int(rewards.get("alcohol", 40))
            oil_reward = int(rewards.get("oil", 18))
            if not self._consume_first(index, {"alcohol"}, alcohol_reward):
                if not self._consume_first(index, {"oil"}, oil_reward):
                    paper_reward = int(rewards.get("paper", 18))
                    self._consume_first(index, {"sandpaper", "paper"}, paper_reward, reason="burned")
        elif script == "acetone": self._consume_first(index, {"water"}, 9)
        elif script == "growth_magic" and neighbors and self._chance(float(definition.get("growth_chance", 0.05))): self._permanent_bonus(self._board[self.r.choice(neighbors)], 1)
        elif script == "shovel": self._destroy_matching(index, tags={"grass"}, reward_each=int(definition.get("destroy_reward_each", 7)))
        elif script == "pickaxe": self._pickaxe(index)
        elif script == "sandpaper":
            metals = [n for n in neighbors if self._has_tag(self._board[n], "metal")]
            if metals:
                self._permanent_bonus(self._board[self.r.choice(metals)], 1)
                reward = int(definition.get("trigger_reward_gold", 0))
                if reward:
                    self._gain_gold(reward, definition["name"])
                trigger_count = int(inst.flags.get("sandpaper_triggers", 0)) + 1
                inst.flags["sandpaper_triggers"] = trigger_count
                if trigger_count >= int(definition.get("trigger_limit", 2)):
                    self._remove(inst, "used", index)
        elif script == "flame": self._flame(index)
        elif script == "apprentice":
            for n in list(neighbors):
                target = self._board[n]
                if self._has_tag(target, "glass") and self._chance(0.2): self._remove(target, "shattered", n, payout_multiplier=7)
        elif script == "rust":
            metals = [n for n in neighbors if self._has_tag(self._board[n], "metal")]
            if metals and self._chance(0.1):
                self._permanent_bonus(self._board[self.r.choice(metals)], -1); self._permanent_bonus(inst, 1)
        elif script == "upgrade_magic" and inst.age >= 10:
            self._transform(inst, self._draw_definition("ingredient", 2))
        elif script == "easter_egg" and self._chance(0.1):
            rarity = self.roll_rarity("ingredient")
            self._transform(inst, self._draw_definition("ingredient", rarity))
        elif script == "paper" and not inst.flags.get("grown") and self._chance(float(definition.get("growth_chance", 0.42))):
            self._permanent_bonus(inst, 1); inst.flags["grown"] = True
        elif script == "herb":
            herbs = [n for n in neighbors if self._board[n].def_id == "herb"]
            if herbs:
                other = self._board[herbs[0]]; self._remove(other, "combined", herbs[0]); self._remove(inst, "combined", index); self._spawn_random(tag="potion", source=index, origin="ingredient")
        elif script == "mercenary":
            targets = [n for n in neighbors if self._has_tag(self._board[n], "monster")]
            if targets and self._remove(self._board[targets[0]], "killed", targets[0]):
                self._gain_gold(int(definition.get("reward_gold", 10)), "佣兵")
                self._remove(inst, "used", index)
        elif script == "warrior":
            for n in list(neighbors):
                target = self._board[n]
                if self._present(target) and self._has_tag(target, "monster") and self._remove(target, "killed", n):
                    self._permanent_bonus(inst, 1)
        elif script == "one_time_growth":
            threshold = int(self.catalog.ingredients[inst.def_id].get("growth_after", 10))
            if inst.age >= threshold and not inst.flags.get("grown"):
                self._permanent_bonus(inst, int(self.catalog.ingredients[inst.def_id].get("growth_amount", 1))); inst.flags["grown"] = True
        elif script == "double_potion":
            self._remove(inst, "potion", index)
        elif script == "copy_potion":
            if neighbors:
                copied = self._board[self.r.choice(neighbors)].def_id
                copies = self._potion_effect_multiplier()
                created = 0
                for _ in range(copies):
                    result = self._generated_ingredient(copied, source=index)
                    if result:
                        created += 1
                        self.s.stats["recent_copied"] = result.def_id
                if created:
                    self.emit("copied", created)
            self._remove(inst, "potion", index)
        elif script == "removal_magic" and not self.negative_disabled() and neighbors and self._chance(0.3, negative=True, source_index=index):
            self._values[self.r.choice(neighbors)] = 0
        elif script == "destroy_magic" and neighbors and self._chance(0.3):
            target_index = self.r.choice(neighbors)
            self._remove(self._board[target_index], "destroyed", target_index)
        elif script == "blank_magic" and not self.negative_disabled() and self._chance(0.3, negative=True, source_index=index): self.s.flags["blank_choice"] = True
        elif script == "greed_magic" and not self.negative_disabled() and self._chance(0.3, negative=True, source_index=index): self.s.flags["force_choose"] = True
        elif script == "alchemy_scrap":
            if int(self.catalog.ingredients[inst.def_id]["base"]) + inst.permanent_bonus >= 5:
                self._remove(inst, "used", index, fixed_payout=20)
        elif script == "merchant" and inst.age % 10 == 0:
            self._gain_gold(-5, "商人")
            item_id = self._draw_available_item(1)
            if item_id is not None:
                self.add_item(item_id)
        elif script == "pendulum":
            if self.s.spin % 2: self._gain_gold(4, "钟摆")
            if inst.age % 20 == 0: self._permanent_bonus(inst, 1)
        elif script == "broken_flask" and self._chance(0.1): self._remove(inst, "shattered", index, fixed_payout=12)
        elif script == "gambler":
            coins = [self._board[n] for n in neighbors if self._board[n].def_id == "coin"]
            if coins:
                if any(x.flags.get("coin_success") for x in coins): self._permanent_bonus(inst, 1)
                else: self._values[index] = 0
        elif script == "gamble_stone":
            roll = self.r.random(); self.emit("chance_checked")
            if roll < 0.1: self._permanent_bonus(inst, 3)
            elif roll < 0.2: inst.permanent_bonus = -int(self.catalog.ingredients[inst.def_id]["base"])
        elif script == "reverse_gear":
            for n in neighbors: self._board[n].counter = max(0, self._board[n].counter - 1)
        elif script == "fast_gear":
            for n in neighbors: self._board[n].counter += 1
        elif script == "alcohol": self._alcohol(index, inst)
        elif script == "golden_key": self._golden_key(index, inst)
        elif script == "locksmith":
            if self._consume_first(index, tags={"chest"}, reward=0, opened=True): self._permanent_bonus(inst, 1)
        elif script in {"gardener","zookeeper","butcher","gem_merchant","arcane_beast"}: self._consumer_core(index, inst, script)
        elif script == "repeater":
            previous = self.s.stats.get("last_potion")
            if previous and not inst.flags.get(f"repeat:{self.s.spin}"):
                self._apply_potion_payload(previous, "复读机", source_index=index); inst.flags[f"repeat:{self.s.spin}"] = True
        elif script == "master_craftsman" and inst.age % 10 == 0:
            for n in neighbors:
                if self._has_tag(self._board[n], "equipment"): self._permanent_bonus(self._board[n], 1)
        elif script == "vein" and inst.age % 8 == 0:
            self._spawn_random(tag="ore", source=index, minimum_rarity=2, origin="ingredient"); self._permanent_bonus(inst, 1); self.emit("periodic")
        elif script == "super_bomb":
            for n in list(neighbors): self._remove(self._board[n], "exploded", n, payout_multiplier=7)
            self._remove(inst, "used", index)

    def _consume_first(self, index: int, ids: set[str] | None = None, reward: int = 0, *, tags: set[str] | None = None, opened: bool = False, reason: str | None = None) -> bool:
        ids = ids or set(); tags = tags or set()
        for n in self._neighbors(index):
            target = self._board[n]
            if not self._present(target):
                continue
            definition = self.catalog.ingredients[target.def_id]
            if target.def_id in ids or set(definition.get("tags", [])).intersection(tags):
                removal_reason = reason or ("opened" if opened or "chest" in definition.get("tags", []) else "consumed")
                if not self._remove(target, removal_reason, n):
                    return False
                if reward: self._gain_gold(reward, self.catalog.ingredients[self._board[index].def_id]["name"])
                return True
        return False

    def _destroy_matching(self, index: int, tags: set[str], reward_each: int) -> int:
        count = 0
        for n in list(self._neighbors(index)):
            if self._present(self._board[n]) and any(self._has_tag(self._board[n], tag) for tag in tags):
                if self._remove(self._board[n], "removed", n): count += 1
        if count: self._gain_gold(count * reward_each, self.catalog.ingredients[self._board[index].def_id]["name"])
        return count

    def _pickaxe(self, index: int) -> None:
        for n in list(self._neighbors(index)):
            target = self._board[n]
            if self._present(target) and self._has_tag(target, "stone"):
                gem = target.def_id == "gem_ore"
                if not self._remove(target, "mined", n):
                    continue
                self._gain_gold(10, "稿子")
                if gem:
                    for _ in range(3): self._spawn_random(tag="ore", minimum_rarity=2, source=index, origin="ingredient")
                else: self._spawn_random(tag="metal", source=index, origin="ingredient")

    def _flame(self, index: int) -> None:
        for n in list(self._neighbors(index)):
            target = self._board[n]
            if not self._present(target): continue
            if target.def_id == "alcohol":
                if self._remove(target, "burned", n):
                    self._gain_gold(50, "火焰")
            elif self._has_tag(target, "wood"):
                value = self._values[n] if n < len(self._values) else 0
                if not self._remove(target, "burned", n):
                    continue
                self._gain_gold(value * 10, "火焰"); self._generated_ingredient("ash", source=index)
                for j in self._neighbors(index):
                    if self._present(self._board[j]) and self._board[j].def_id == "furnace_core": self._permanent_bonus(self._board[j], 1)

    def _alcohol(self, index: int, inst: IngredientInstance) -> None:
        for n in list(self._neighbors(index)):
            target = self._board[n]
            if self._present(target) and (target.def_id in {"water","alcohol"} or self._has_tag(target, "organic_liquid")):
                if self._remove(target, "consumed", n): self._permanent_bonus(inst, 1)

    def _golden_key(self, index: int, inst: IngredientInstance) -> None:
        for n in self._neighbors(index):
            target = self._board[n]
            if self._present(target) and self._has_tag(target, "chest"):
                if self._remove(target, "opened", n, payout_multiplier=2):
                    self._remove(inst, "used", index)
                return

    def _consumer_core(self, index: int, inst: IngredientInstance, script: str) -> None:
        mapping = {
            "gardener": ({"plant"}, set()), "zookeeper": ({"animal"}, set()), "butcher": ({"human"}, set()),
            "gem_merchant": ({"ore","metal"}, set()), "arcane_beast": ({"magic"}, {"white_mage","witch"})}
        tags, ids = mapping[script]
        for n in list(self._neighbors(index)):
            target = self._board[n]
            if self._present(target) and (target.def_id in ids or any(self._has_tag(target, tag) for tag in tags)):
                if target.uid != inst.uid and self._remove(target, "consumed", n): self._permanent_bonus(inst, 1)

    def _trigger_potion(self, index: int, inst: IngredientInstance, potion: dict[str, Any]) -> None:
        self._apply_potion_payload(potion, self.catalog.ingredients[inst.def_id]["name"], source_index=index)
        self.s.stats["last_potion"] = dict(potion)
        self._remove(inst, "potion", index)

    def _apply_potion_payload(self, potion: dict[str, Any], source: str, *, source_index: int | None = None) -> None:
        multiplier = self._potion_effect_multiplier()
        if potion.get("gold"): self._gain_gold(int(potion["gold"]) * multiplier, source)
        if potion.get("token"): self._gain_token(potion["token"], int(potion.get("amount", 1)) * multiplier, source)
        if potion.get("flag"): self.s.flags[potion["flag"]] = True
        minimum = potion.get("choice_minimum")
        if minimum:
            self.s.flags["choice_minimum_rarity"] = max(
                int(self.s.flags.get("choice_minimum_rarity", 0)),
                int(minimum.get("minimum", 1)),
            )
            self.s.flags["choice_minimum_count"] = int(self.s.flags.get("choice_minimum_count", 0)) + int(minimum.get("count", 1)) * multiplier
        if potion.get("item_rarity"):
            for _ in range(multiplier):
                item_id = self._draw_available_item(int(potion["item_rarity"]))
                if item_id is None:
                    break
                self.add_item(item_id)
        if potion.get("recycle") and self.s.removed_history:
            for _ in range(multiplier):
                self._generated_ingredient(self.r.choice(self.s.removed_history), source=source_index)
        if potion.get("purify"):
            choices = [x for x in self.s.ingredients if int(self.catalog.ingredients[x.def_id].get("rarity", 0)) == 1]
            for target in self.r.sample(choices, min(len(choices), multiplier)):
                self._remove(target, "purified", None)

    def _consume_choice_guarantee(self, choice: PendingChoice) -> None:
        if choice.minimum_rarity is None:
            return
        remaining = max(0, int(self.s.flags.get("choice_minimum_count", 0)) - 1)
        reserved = max(0, int(self.s.flags.get("choice_minimum_reserved", 0)) - 1)
        # bind() supplies these zero defaults for old saves. Keep the same
        # representation after consumption so stateless reloads do not alter
        # the observable payload of an otherwise identical game.
        self.s.flags["choice_minimum_count"] = remaining
        self.s.flags["choice_minimum_reserved"] = reserved
        if not remaining:
            self.s.flags["choice_minimum_rarity"] = 0
    @staticmethod
    def _as_rule_list(value: Any) -> list[dict[str, Any]]:
        if not value:
            return []
        if isinstance(value, dict):
            return [value]
        return [row for row in value if isinstance(row, dict)]

    def _spawn_is_mineral(self, tag: str | None, def_id: str | None) -> bool:
        if tag in MINERAL_TAGS:
            return True
        if def_id and def_id in self.catalog.ingredients:
            return bool(MINERAL_TAGS.intersection(self.catalog.ingredients[def_id].get("tags", [])))
        return False

    def _spawn_minimum_from_items(
        self,
        *,
        tag: str | None,
        def_id: str | None,
        minimum_rarity: int,
    ) -> tuple[int, list[str]]:
        """Combine declarative item minimums for one successful spawn.

        The returned keys are marked only after ``add_ingredient`` succeeds,
        which keeps once-per-round guarantees from being consumed by failed
        unique/duplicate generation attempts.
        """
        if not self._spawn_is_mineral(tag, def_id):
            return int(minimum_rarity), []
        effective = int(minimum_rarity)
        once_keys: list[str] = []
        for item_id in self.s.items:
            rules = self._as_rule_list(self.catalog.items[item_id].get("spawn_minimum_rarity"))
            for rule_index, rule in enumerate(rules):
                target_tags = set(rule.get("tags", []))
                actual_tags = set(self.catalog.ingredients[def_id].get("tags", [])) if def_id else set()
                if target_tags:
                    if tag not in target_tags and not target_tags.intersection(actual_tags):
                        continue
                if rule.get("tag") and rule["tag"] != tag and rule["tag"] not in actual_tags:
                    continue
                key = f"spawn_minimum:{item_id}:{rule_index}"
                if rule.get("once_per_round") and self._round_events.get(key):
                    continue
                effective = max(effective, int(rule.get("minimum", 1)))
                if rule.get("once_per_round"):
                    once_keys.append(key)
        return effective, once_keys

    def _spawn_random(
        self,
        *,
        tag: str | None = None,
        rarity: int | None = None,
        minimum_rarity: int = 1,
        minimum_rarity_chance: dict[str, Any] | None = None,
        def_id: str | None = None,
        source: int | None = None,
        exclude: set[str] | None = None,
        origin: str = "system",
    ) -> IngredientInstance | None:
        if origin == "ingredient" and self.ingredient_generation_disabled():
            return None
        if def_id is not None and exclude and def_id in exclude:
            def_id = None
        effective_minimum, once_keys = self._spawn_minimum_from_items(
            tag=tag,
            def_id=def_id,
            minimum_rarity=minimum_rarity,
        )
        # A stronger deterministic minimum makes a weaker probabilistic
        # minimum redundant.  Skipping that redundant roll also prevents a
        # table + vein combination from adding an extra RNG event.
        if minimum_rarity_chance:
            chance_minimum = int(minimum_rarity_chance.get("minimum", 1))
            chance = float(minimum_rarity_chance.get("chance", 0.0))
            if effective_minimum < chance_minimum and self.r.random() < chance:
                effective_minimum = chance_minimum
        if def_id is None:
            if rarity is None or int(rarity) < effective_minimum:
                rarity = self.roll_rarity("ingredient", minimum=effective_minimum)
            def_id = self._draw_definition("ingredient", int(rarity), tag=tag, exclude=exclude, minimum=effective_minimum)
        elif effective_minimum > int(self.catalog.ingredients[def_id].get("rarity", 0)):
            # An explicit low-rarity mineral cannot violate a stronger
            # generated minimum; draw another definition in the same family.
            family = tag or next((name for name in self.catalog.ingredients[def_id].get("tags", []) if name in MINERAL_TAGS), None)
            def_id = self._draw_definition("ingredient", effective_minimum, tag=family, exclude=exclude, minimum=effective_minimum)
        created = self.add_ingredient(def_id)
        if created:
            for key in once_keys:
                self._round_events[key] = 1
            self._after_successful_generation(created, source=source, origin=origin)
        return created

    def _transform(self, inst: IngredientInstance, into: str) -> None:
        old = inst.def_id
        self._apply_generation_bonus_to_instance(inst)
        applied_generation_bonus = int(inst.flags.get("ingredient_generation_bonus_applied", 0))
        inst.def_id = into
        inst.age = 0; inst.counter = 0
        inst.flags = {"ingredient_generation_bonus_applied": applied_generation_bonus} if applied_generation_bonus else {}
        self._apply_generation_bonus_to_instance(inst)
        try:
            index = next(i for i, current in enumerate(self._board) if current.uid == inst.uid)
        except StopIteration:
            index = None
        self.emit("transformed", source_index=index)
        self.s.last_log.append(f"{self.catalog.ingredients[old]['name']}变化为{self.catalog.ingredients[into]['name']}。")

    def _permanent_bonus(self, inst: IngredientInstance, amount: int) -> None:
        if not self._present(inst):
            return  # Departed slots cannot grow or emit growth rewards.
        if self.s.fun_mode == "minimal" and amount:
            amount *= 2
        inst.permanent_bonus += amount
        self.emit("permanent_bonus")
        if self._has_tag(inst, "liquid"): self.emit("liquid_permanent_bonus")
        if amount > 0 and "reaction_echo" in self.s.items and not self._round_events.get("reaction_echo"):
            self._round_events["reaction_echo"] = 1
            self._gain_gold(amount, "反应残响")
        if amount > 0:
            self._trigger_context_essences("permanent_bonus", instance=inst, amount=amount)

    def _prevent_countdown(self, board_index: int, inst: IngredientInstance) -> bool:
        if "preservative_box" in self.s.items and not inst.flags.get("preserved"):
            inst.flags["preserved"] = True; inst.age = max(0, inst.age - 1); self._permanent_bonus(inst, 1); self.emit("countdown_prevented"); return True
        return False

    def _remove(self, inst: IngredientInstance, reason: str, board_index: int | None, *, payout_multiplier: int = 1, fixed_payout: int | None = None) -> bool:
        if not self._present(inst): return False
        if board_index is not None and reason in {"shattered","removed","consumed","killed","exploded","opened","burned","destroyed","rapid"}:
            for n in self._neighbors(board_index):
                substitute = self._board[n]
                if self._present(substitute) and substitute.def_id == "scapegoat" and substitute.uid != inst.uid and not substitute.flags.get(f"saved:{self.s.spin}"):
                    substitute.flags[f"saved:{self.s.spin}"] = True
                    self._remove(
                        substitute,
                        "sacrificed",
                        n,
                        fixed_payout=int(self.catalog.ingredients[substitute.def_id].get("sacrifice_reward_gold", 0)),
                    )
                    return False
            for n in self._neighbors(board_index):
                guard = self._board[n]
                if not self._present(guard):
                    continue
                if self._present(guard) and guard.def_id == "restraint" and not guard.flags.get(f"guard:{self.s.spin}"):
                    guard.flags[f"guard:{self.s.spin}"] = True; self._permanent_bonus(guard, 1); return False
                expert = self.catalog.ingredients[guard.def_id]
                aura = expert.get("aura", {})
                if reason == "shattered" and aura.get("protect") == "shattered" and self._has_tag(inst, "equipment"): return False
        definition = self.catalog.ingredients[inst.def_id]
        for item_id in self.s.items:
            protection = self.catalog.items[item_id].get("protect_once", {})
            if reason != protection.get("reason"):
                continue
            if protection.get("tag") and not self._has_tag(inst, str(protection["tag"])):
                continue
            if protection.get("id") and protection["id"] != inst.def_id:
                continue
            if not set(protection.get("tags_all", [])).issubset(definition.get("tags", [])):
                continue
            key = str(protection.get("flag", f"protection_used:{item_id}"))
            if inst.flags.get(key):
                continue
            inst.flags[key] = True
            if protection.get("event"):
                self.emit(str(protection["event"]))
            return False
        for item_id in self.s.items:
            protection = self.catalog.items[item_id].get("protect", {})
            if reason == protection.get("reason") and (protection.get("id") == inst.def_id): return False
        payout = fixed_payout if fixed_payout is not None else 0
        if payout_multiplier > 1 and board_index is not None and board_index < len(self._values): payout += self._values[board_index] * payout_multiplier
        if inst.stored_gold: payout += inst.stored_gold
        on_removed = definition.get("on_removed", {})
        on_removed_matches = bool(on_removed and on_removed.get("reason") in {"any", reason})
        if on_removed_matches:
            payout += int(on_removed.get("gold", 0)) * payout_multiplier
        self.s.ingredients = [x for x in self.s.ingredients if x.uid != inst.uid]
        self.s.removed_history.append(inst.def_id)
        self._removed_values.append((int(definition.get("rarity", 0)), int(definition.get("base", 0))))
        if payout: self._gain_gold(payout, f"移除{definition['name']}")
        event_index = board_index
        if event_index is None:
            event_index = next((i for i, member in enumerate(self._board) if member.uid == inst.uid), None)
        self.emit("removed", source_index=event_index, source_tags=definition.get("tags", []))
        for tag in definition.get("tags", []): self.emit(f"removed_tag:{tag}")
        if reason == "opened": self.emit("opened")
        if reason == "shattered": self.emit("shattered", source_index=event_index, source_tags=definition.get("tags", []))
        if reason == "burned": self.emit("burned")
        if reason == "potion": self.emit("potion", source_index=event_index, source_tags=definition.get("tags", []))
        if on_removed_matches:
            for token, amount in on_removed.get("tokens", {}).items():
                self._gain_token(str(token), int(amount), definition["name"])
        if inst.def_id in {"ash", "rust", "alchemy_scrap"}:
            self.emit("removed_ids:ash,rust,alchemy_scrap")
        if on_removed_matches and on_removed.get("spawn_tag"):
            self._spawn_random(
                tag=on_removed["spawn_tag"],
                exclude=set(on_removed.get("exclude", [])),
                source=event_index,
                origin="ingredient",
            )
        if on_removed_matches and on_removed.get("item_rarity"):
            item_id = self._draw_available_item(int(on_removed["item_rarity"]))
            if item_id is not None:
                self.add_item(item_id)
        if on_removed_matches and on_removed.get("item_random"):
            item_id = self._draw_available_item(None)
            if item_id is not None:
                self.add_item(item_id)
        if on_removed_matches and on_removed.get("universal_chest"):
            item_id = self._draw_available_item(3)
            if item_id is not None:
                self.add_item(item_id)
            for token in ("remove","roll","essence"): self._gain_token(token, 1, "万能箱")
        if inst.def_id == "nine_lives_cat" and int(inst.flags.get("lives", 8)) > 0:
            new = self._generated_ingredient("nine_lives_cat", source=event_index)
            if new: new.flags["lives"] = int(inst.flags.get("lives", 8)) - 1
        if "equivalent_exchange" in self.s.items and not self._round_events.get("equivalent_exchange"):
            candidates = [x for x in self.s.ingredients if int(self.catalog.ingredients[x.def_id].get("rarity", 0)) == int(definition.get("rarity", 0))]
            if candidates:
                self._permanent_bonus(self.r.choice(candidates), int(definition.get("base", 0))); self._round_events["equivalent_exchange"] = 1
        return True

    def _gain_token(
        self,
        token: str,
        amount: int,
        source: str,
        *,
        _mode_processed: bool = False,
    ) -> None:
        """Add tokens while applying the active entertainment-mode transform."""
        amount = int(amount)
        if amount <= 0:
            return
        if not _mode_processed:
            if self.s.fun_mode == "giant" and token == "remove":
                amount *= 2
            elif self.s.fun_mode == "rapid" and token == "roll":
                amount *= 2
            elif self.s.fun_mode == "blind_box" and token == "roll":
                # Each original roll token is converted independently.  The
                # recursive calls bypass this branch, preventing conversion
                # of the resulting Delete/Essence tokens.
                for _ in range(amount):
                    converted = "remove" if self.r.random() < 0.5 else "essence"
                    self._gain_token(converted, 1, source, _mode_processed=True)
                return
        self.s.tokens[token] = int(self.s.tokens.get(token, 0)) + amount
        self.emit("token", amount)
        self.s.last_log.append(f"{source}：获得{amount}个{token} Token。")

    def gain_token(self, token: str, amount: int, source: str) -> None:
        """Public alias for the shared token-gain entry point."""
        self._gain_token(token, amount, source)

    def _run_round_conditions(self, income: int) -> None:
        present_board = [x for x in self._board if self._present(x)]
        ids = [x.def_id for x in present_board]
        tag_counts: Counter[str] = Counter(tag for x in present_board for tag in self.catalog.ingredients[x.def_id].get("tags", []))
        for item_id in self.s.items:
            condition = self.catalog.items[item_id].get("round_condition")
            if not condition: continue
            once_key = f"round_condition:{item_id}"
            if self._round_events.get(once_key):
                continue
            ok = True
            if condition.get("event"): ok = self._round_events.get(condition["event"], 0) > 0
            if condition.get("tag_count"): ok = tag_counts[condition["tag_count"]["tag"]] >= int(condition["tag_count"]["count"])
            if condition.get("pool_size") is not None: ok = len(self.s.ingredients) >= int(condition["pool_size"])
            if condition.get("empty_slots") is not None:
                capacity = self.board_capacity()
                ok = capacity - len(present_board) >= int(condition["empty_slots"])
            if condition.get("same_count"): ok = max(Counter(ids).values(), default=0) >= int(condition["same_count"])
            if condition.get("all_unique"): ok = len(ids) == len(set(ids))
            if condition.get("has_zero"): ok = any(value == 0 for value, instance in zip(self._values, self._board) if self._present(instance))
            if condition.get("income_multiple"): ok = income % int(condition["income_multiple"]) == 0
            if condition.get("income_even"): ok = income % 2 == 0
            if condition.get("adjacent_same"): ok = self._has_adjacent_same(int(condition["adjacent_same"]))
            if ok:
                self._round_events[once_key] = 1
                self._record_item_trigger(item_id)
                self.emit(f"item_trigger:{item_id}")
                self._gain_gold(int(condition.get("gold", 0)), self.catalog.items[item_id]["name"])

    def _has_adjacent_same(self, count: int) -> bool:
        for i, inst in enumerate(self._board):
            if not self._present(inst):
                continue
            same = 1 + sum(1 for n in self._neighbors(i) if self._present(self._board[n]) and self._board[n].def_id == inst.def_id)
            if same >= count: return True
        return False

    def _trigger_pigment_pair_essences(self) -> None:
        """Trigger the first same-colour pigment pair in stable board order."""
        for index, instance in enumerate(self._board):
            definition = self.catalog.ingredients[instance.def_id]
            color = definition.get("pigment_color")
            if color is None or not self._present(instance):
                continue
            if any(
                self.catalog.ingredients[self._board[n].def_id].get("pigment_color") == color
                for n in self._neighbors(index)
                if n < len(self._board) and self._present(self._board[n])
            ):
                self._trigger_context_essences("adjacent_same", instance=instance)
                return

    def _store_item_gold(self, item_id: str, amount: int, source: str) -> None:
        if amount <= 0:
            return
        storage = self.s.stats.setdefault("item_storage", {})
        storage[item_id] = int(storage.get(item_id, 0)) + int(amount)
        self.s.last_log.append(f"{source}：储存{amount}g。")

    def _withdraw_order_savings(self) -> int:
        storage = self.s.stats.setdefault("item_storage", {})
        withdrawn = 0
        for item_id, balance in list(storage.items()):
            item = self.catalog.items.get(item_id, {})
            if not item.get("order_savings", {}).get("withdraw_before_failure"):
                continue
            amount = max(0, int(balance))
            if not amount:
                continue
            storage[item_id] = 0
            withdrawn += amount
            self._gain_gold(amount, item.get("name", item_id))
        return withdrawn

    def _settle_order(self) -> None:
        amount, _ = self.current_order()
        used_emergency_protocol = False
        if self.s.gold < amount:
            self._withdraw_order_savings()
        if self.s.gold < amount and "emergency_coffee" in self.s.items:
            self.s.items.remove("emergency_coffee"); self._record_item_trigger("emergency_coffee"); self.s.spins_left = 1; self.emit("coffee_used"); self.s.last_log.append("紧急咖啡提供了额外1回合。")
            return
        if self.s.gold < amount and "emergency_protocol" in self.s.items:
            self.s.items.remove("emergency_protocol"); self._record_item_trigger("emergency_protocol"); self.s.gold = amount
            used_emergency_protocol = True
            self.emit("emergency_protocol_used")
        if self.s.gold < amount:
            self.s.status = "lost"; self.s.last_log.append(f"订单失败：需要{amount}g，当前只有{self.s.gold}g。")
            return
        self.s.gold -= amount
        # The just-paid order consumes its old penalty. A rescue creates a
        # distinct obligation for the next order, not the one paid above.
        self.s.flags.pop("next_order_penalty", None)
        if used_emergency_protocol:
            self.s.flags["next_order_penalty"] = True
        in_endless = bool(self.s.endless_mode)
        in_peace = bool(self.s.peace_mode)
        if in_peace:
            # Mainline progress remains anchored at the final order while
            # peace orders are tracked independently.
            completed = self.s.order_index + self.s.peace_order + 1
        else:
            completed = self.s.order_index + 1
            self.s.order_index = completed
        self.s.stats["last_completed_order_amount"] = amount
        self.emit("order_completed", value=amount)
        if self.s.fun_mode == "minimal":
            successful = int(self.s.stats.get("minimal_successful_orders", 0)) + 1
            self.s.stats["minimal_successful_orders"] = successful
            if successful % 2 == 0:
                self._gain_token("remove", 1, "极简模式每两单奖励")
        for item_id in list(self.s.items):
            savings = self.catalog.items[item_id].get("order_savings")
            if savings:
                deposit = int(savings.get("deposit_on_complete", 0))
                if deposit:
                    self._store_item_gold(item_id, deposit, self.catalog.items[item_id]["name"])
        if "alchemy_bank" in self.s.items:
            returned = int(float(self.s.stats.pop("bank_balance", 0)) * 1.5)
            if returned:
                self._gain_gold(returned, "炼金银行"); self.emit("bank_return", value=returned)
            deposit = max(0, self.s.gold // 10)
            if deposit:
                self.s.gold -= deposit; self.s.stats["bank_balance"] = deposit
                self.s.last_log.append(f"炼金银行存入{deposit}g。")
        self.check_essences()
        if "double_ledger" in self.s.items: self.s.flags["double_next_income"] = True
        token_amounts = self.token_reward_amounts(self.s.difficulty)
        if completed >= 4 and completed % 2 == 0:
            for token, token_amount in token_amounts.items(): self._gain_token(token, token_amount, "订单周期奖励")
        if in_peace:
            self.s.peace_order += 1
        elif not in_endless:
            deduction = self.post_order_gold_deduction(completed)
            if deduction:
                actual = min(max(0, int(self.s.gold)), deduction)
                self.s.gold -= actual
                self.s.last_log.append(f"难度规则：订单结算后额外扣除{actual}g。")
        if self._check_peace_goal():
            return
        self.s.pending.extend(self._order_rewards(completed))
        essence_tokens = int(self.s.tokens.get("essence", 0))
        self.s.tokens["essence"] = 0
        for _ in range(essence_tokens):
            choice = self.make_choice("essence", source="essence_token")
            if choice.offers: self.s.pending.append(choice)
        if self.s.endless_mode:
            self._advance_endless_order()
            self.s.last_log.append(
                f"完成无限订单{self.s.endless_order - 1}，支付{amount}g；下一份无限订单需要{self.s.endless_target}g。"
            )
        elif self.s.peace_mode:
            self.s.flags.pop("next_order_penalty", None)
            self.s.spins_left = 7
            self.s.last_log.append(
                f"完成和平订单{self.s.peace_order}，下一份和平订单仍为0g/7回合。"
            )
        elif self._mainline_complete(completed, self.s.difficulty):
            self.s.pending.append(
                PendingChoice(
                    kind="run_end",
                    offers=["end_run", "enter_endless", "enter_peace"],
                    can_skip=False,
                    source="mainline_complete",
                )
            )
            self.s.last_log.append("主线订单已完成：请选择结束本局、进入无限模式或进入和平模式。")
        elif self.s.status == "won":
            self.s.last_log.append(f"已完成第{completed}份订单：本局胜利。仍可查看状态与库存。")
        else:
            _, spins = self.current_order()
            self.s.spins_left = spins
            self.s.last_log.append(f"完成第{completed}份订单，支付{amount}g。")

    def _advance_endless_order(self) -> None:
        """Advance an already-active infinite run after a paid order."""
        completed = int(self.s.endless_order)
        self.s.stats["endless_orders_completed"] = int(
            self.s.stats.get("endless_orders_completed", 0)
        ) + 1
        self.s.endless_order = completed + 1
        self.s.endless_target = self.next_endless_target(self.s.endless_target)
        self.s.stats["highest_endless_order"] = max(
            int(self.s.stats.get("highest_endless_order", 0)), self.s.endless_order
        )
        self.s.spins_left = 10

    def _resolve_run_end_choice(self, selected: str) -> None:
        if selected == "end_run":
            self.s.status = "won"
            self.s.last_log.append("已完成主线订单：本局胜利。")
            return
        if selected == "enter_peace":
            self.s.peace_mode = True
            self.s.peace_order = 0
            self.s.endless_mode = False
            self.s.endless_order = 0
            self.s.endless_target = 0
            self.s.spins_left = 7
            self.s.flags.pop("next_order_penalty", None)
            self.s.last_log.append("已进入和平模式：和平订单为0g/7回合，达到1000000g时胜利。")
            # A run may already hold the peace-mode target when the player
            # enters the mode (for example after a high-value final order).
            # Apply the same goal check immediately so the terminal state is
            # not delayed until an otherwise unnecessary spin.
            self._check_peace_goal()
            return
        if selected != "enter_endless":
            raise GameError("无效的结算模式选择")
        self.s.peace_mode = False
        self.s.peace_order = 0
        self.s.endless_mode = True
        self.s.endless_order = 1
        self.s.endless_target = 1000
        self.s.spins_left = 10
        self.s.stats["highest_endless_order"] = max(
            int(self.s.stats.get("highest_endless_order", 0)), 1
        )
        self.s.last_log.append("已进入无限模式：无限订单1，需要1000g，限时10回合。")

    def _order_rewards(self, completed: int) -> list[PendingChoice]:
        rewards: list[PendingChoice] = []
        if completed <= 5: minimums = [2,2,2]
        elif completed <= 8: minimums = [3]
        elif completed == 9: minimums = [3,3]
        else: minimums = [3,3,3]
        rewards.append(self.make_choice("ingredient", minimums=minimums, source="order_guarantee"))
        rewards.extend(self._item_choice_rewards_for_event("order_completed"))
        item_minimums = [3] if self.s.flags.pop("order_book_reward", False) else None
        rewards.append(self.make_choice("item", minimums=item_minimums, source="order"))
        return rewards

    def choose(self, number: int) -> str:
        self._require_action_window(allow_pending=True)
        if not self.s.pending: raise GameError("当前没有待选奖励")
        choice = self.s.pending[0]
        if not 1 <= number <= len(choice.offers): raise GameError("选择序号超出范围")
        selected = choice.offers[number - 1]
        # Start the public action's log before its effects, not afterwards.
        # Otherwise acquisition/removal listeners disappear or old action
        # messages are copied into this one. Keep the entire current chain.
        self.s.last_log = []
        self.s.pending.pop(0)
        self._consume_choice_guarantee(choice)
        if choice.kind == "ingredient":
            self.add_ingredient(selected)
            self.emit("ingredient_chosen")
            if choice.source in {"large_material_pack","giant_material_pack","alchemy_supply_pack"}: self.emit("pack_choice")
            label = self.catalog.ingredients[selected]["name"]
        elif choice.kind == "item":
            self.add_item(selected); label = self.catalog.items[selected]["name"]
        elif choice.kind == "run_end":
            self._resolve_run_end_choice(selected)
            label = RUN_END_OPTIONS[selected]["name"]
        elif choice.kind == "bundle":
            option = choice.details.get("options", {}).get(selected, {})
            if not isinstance(option, dict):
                raise GameError("无效的组合选择")
            for def_id in option.get("add_ingredients", []):
                self.add_ingredient(str(def_id))
            label = str(option.get("name", selected))
        else:
            self.add_essence(selected); label = self.catalog.essences[selected]["name"]
        self.s.last_log.insert(0, f"选择了{label}。")
        self._record_choice_resolution(choice.kind, False)
        self.check_essences()
        self._sync_rng()
        return selected

    def skip(self) -> None:
        self._require_action_window(allow_pending=True)
        if not self.s.pending: raise GameError("当前没有待选奖励")
        choice = self.s.pending[0]
        if not choice.can_skip: raise GameError("本次选择不能跳过")
        self.s.last_log = [f"跳过了{choice.kind}选择。"]
        self.s.pending.pop(0)
        self._consume_choice_guarantee(choice)
        self.emit(f"skip_{choice.kind}")
        self._record_choice_resolution(choice.kind, True)
        self.check_essences(); self._sync_rng()

    def reroll(self) -> None:
        self._require_action_window(allow_pending=True)
        if not self.s.pending: raise GameError("当前没有待选奖励")
        if self.s.tokens.get("roll", 0) <= 0: raise GameError("没有Roll Token")
        old = self.s.pending[0]
        if not old.offers:
            raise GameError("没有候选的奖励不能重调，请跳过")
        if old.kind in {"run_end", "bundle"}:
            raise GameError("该选择不能重调")
        if old.kind == "essence": raise GameError("精粹选择不能重调")
        self.s.last_log = ["消耗1个Roll Token，候选已重调。"]
        self._ensure_choice_identity(old)
        choice_events = dict(old.details.get("event_counts", {}))
        choice_events["reroll"] = int(choice_events.get("reroll", 0)) + 1
        self.s.tokens["roll"] -= 1; self.emit("token_spent"); self.emit("reroll")
        constraints = old.details.get("draw_constraints", {})
        new = self.make_choice(old.kind, count=len(old.offers), source=old.source, can_skip=old.can_skip, guarantee_rarity=old.minimum_rarity, tag_filter=old.tag_filter, fixed_rarity=constraints.get("fixed_rarity"), minimums=constraints.get("minimums"), _redraw=True)
        if "lab_membership" in self.s.items:
            attempts = 0
            while set(new.offers) & set(old.offers) and attempts < 20:
                new = self.make_choice(old.kind, count=len(old.offers), source=old.source, can_skip=old.can_skip, guarantee_rarity=old.minimum_rarity, tag_filter=old.tag_filter, fixed_rarity=constraints.get("fixed_rarity"), minimums=constraints.get("minimums"), _redraw=True); attempts += 1
        new.details.update(choice_uid=old.details["choice_uid"], event_counts=choice_events)
        self.s.pending[0] = new
        self._record_choice_events(new)
        self.check_essences(); self._sync_rng()

    def remove(self, index: int) -> str:
        self._require_action_window()
        if self.s.tokens.get("remove", 0) <= 0: raise GameError("没有删除Token")
        if not 1 <= index <= len(self.s.ingredients): raise GameError("成分序号超出范围")
        inst = self.s.ingredients[index - 1]
        definition = self.catalog.ingredients[inst.def_id]
        if not definition.get("removable", True): raise GameError(f"{definition['name']}不能主动删除")
        self.s.last_log = [f"删除了{definition['name']}。"]
        self.s.tokens["remove"] -= 1; self.emit("token_spent")
        self._remove(inst, "manual", None); self.emit("manual_removed")
        if "warehouse_manager" in self.s.items:
            rarity = int(definition.get("rarity", 0)); self._gain_gold(2 if rarity == 1 else (8 if rarity >= 3 else 0), "仓库管理员")
        self.check_essences(); self._sync_rng()
        return inst.def_id

    def toggle_item(self, item_id: str) -> bool:
        """Toggle a data-declared item switch and return its requested state."""
        self._require_action_window()
        if item_id not in self.s.items:
            raise GameError("未持有该道具")
        item = self.catalog.items[item_id]
        flag = item.get("toggle_flag")
        if not flag:
            raise GameError("该道具没有可切换效果")
        current = bool(self.s.flags.get(str(flag), False))
        self.s.flags[str(flag)] = not current
        state = "开启" if not current else "关闭"
        self.s.last_log = [f"{item['name']}已{state}。"]
        self._sync_rng()
        return not current

    def use_item(self, item_id: str) -> None:
        self._require_action_window()
        if item_id not in self.s.items: raise GameError("未持有该道具")
        item = self.catalog.items[item_id]
        active = item.get("active")
        if not active: raise GameError("该道具没有主动效果")
        if active.get("order_book"):
            if self.s.spins_left != 1: raise GameError("幸运订单簿只能在订单剩余1回合时使用")
            self.s.flags["order_book_sacrifice"] = True
            self.s.last_log = ["幸运订单簿已准备：下一回合收益将被放弃。"]
            self._sync_rng()
            return
        self.s.last_log = [f"使用了{item['name']}。"]
        for _ in range(int(active.get("ingredient_choices", 0))): self.s.pending.append(self.make_choice("ingredient", source=item_id))
        for rarity in active.get("fixed_ingredient_choices", []): self.s.pending.append(self.make_choice("ingredient", fixed_rarity=(None if int(rarity) == 0 else int(rarity)), source=item_id))
        for spec in self._as_rule_list(active.get("tagged_ingredient_choices")):
            for _ in range(int(spec.get("count", 1))):
                self.s.pending.append(self.make_choice("ingredient", source=item_id, tag_filter=spec.get("tag")))
        for _ in range(int(active.get("item_choices", 0))): self.s.pending.append(self.make_choice("item", source=item_id))
        for spec in self._as_rule_list(active.get("add_ingredients")):
            for _ in range(int(spec.get("count", 1))):
                self.add_ingredient(str(spec["id"]))
        if active.get("consume"):
            self.s.items.remove(item_id)
            self._record_item_trigger(item_id)
        self._sync_rng()

    def check_essences(self) -> None:
        for essence_id in list(self.s.essences):
            if essence_id not in self.s.essences:
                continue
            data = self.catalog.essences[essence_id]
            if self._trigger_ready(essence_id, data.get("trigger", {})):
                self._trigger_essence(essence_id, data)

    def _trigger_essence(self, essence_id: str, data: dict[str, Any], context: dict[str, Any] | None = None) -> None:
        if essence_id not in self.s.essences or essence_id in self._triggering_essences:
            return
        self._triggering_essences.add(essence_id)
        try:
            self._apply_essence_effect(data.get("effect", {}), data["name"], context=context)
        finally:
            self._triggering_essences.discard(essence_id)
        hits = self.s.stats.setdefault("essence_hits", {})
        hits[essence_id] = int(hits.get(essence_id, 0)) + 1
        uses = 2 if "essence_stabilizer" in self.s.items else 1
        if hits[essence_id] >= uses:
            self.s.essences.remove(essence_id)
            self.s.consumed_essences.append(essence_id)
        else:
            self._snapshot_essence_baseline(essence_id)
            self.s.stats[f"essence_last_trigger:{essence_id}"] = self.s.spin
        self.s.last_log.append(f"{data['name']}触发。")

    def _trigger_context_essences(self, context_type: str, **context: Any) -> None:
        for essence_id in list(self.s.essences):
            if essence_id not in self.s.essences or essence_id in self._triggering_essences:
                continue
            if self.s.stats.get(f"essence_last_trigger:{essence_id}") == self.s.spin:
                continue
            data = self.catalog.essences[essence_id]
            trigger = data.get("trigger", {})
            matched_context = dict(context)
            if context_type == "before_choice":
                spec = trigger.get("before_choice")
                if not spec:
                    continue
                expected_kind = spec.get("kind") if isinstance(spec, dict) else spec
                if expected_kind != context.get("kind"):
                    continue
            elif context_type == "permanent_bonus":
                spec = trigger.get("next_permanent_bonus")
                instance = context.get("instance")
                if not spec or instance is None or int(context.get("amount", 0)) <= 0:
                    continue
                definition = self.catalog.ingredients[instance.def_id]
                if spec.get("tag") not in definition.get("tags", []):
                    continue
            elif context_type == "board_tag_appearance":
                spec = trigger.get("next_board_tag_appearance")
                if not spec:
                    continue
                tag = spec.get("tag")
                targets = [
                    instance for instance in context.get("instances", [])
                    if self._present(instance) and tag in self.catalog.ingredients[instance.def_id].get("tags", [])
                ]
                if not targets:
                    continue
                matched_context["instance"] = targets[0]
            elif context_type == "adjacent_same":
                spec = trigger.get("next_adjacent_same")
                instance = context.get("instance")
                if not spec or instance is None:
                    continue
                definition = self.catalog.ingredients[instance.def_id]
                if spec.get("tag") and spec["tag"] not in definition.get("tags", []):
                    continue
                if spec.get("field") and definition.get(spec["field"]) is None:
                    continue
                matched_context["instance"] = instance
            else:
                continue
            self._trigger_essence(essence_id, data, context=matched_context)

    def _trigger_ready(self, essence_id: str, trigger: dict[str, Any]) -> bool:
        if self.s.stats.get(f"essence_last_trigger:{essence_id}") == self.s.spin:
            return False
        if any(key in trigger for key in ("before_choice", "next_permanent_bonus", "next_board_tag_appearance", "next_adjacent_same")):
            return False
        baseline = self.s.stats.setdefault("essence_baseline", {}).get(essence_id, {"events":{},"values":{},"spin":self.s.spin})
        totals = self.s.stats.setdefault("event_counts", {})
        values = self.s.stats.setdefault("event_values", {})
        board_defs = [self.catalog.ingredients[x.def_id] for x in self._board if self._present(x)]
        if any(key.startswith("board_") for key in trigger) and not self._board and not self.s.stats.get("last_board_topology"):
            # An old/no-draw save has no observable round to satisfy board
            # predicates; in particular an absent board is not "all unique".
            return False
        if "spins" in trigger and self.s.spin - int(baseline.get("spin", self.s.spin)) < int(trigger["spins"]): return False
        if "event" in trigger and int(totals.get(trigger["event"], 0)) <= int(baseline.get("events", {}).get(trigger["event"], 0)): return False
        if "event_count" in trigger:
            spec=trigger["event_count"]; event=spec["event"]
            if int(totals.get(event,0))-int(baseline.get("events",{}).get(event,0)) < int(spec["count"]): return False
        if "event_count_sum" in trigger:
            spec = trigger["event_count_sum"]
            total = sum(
                int(totals.get(event, 0)) - int(baseline.get("events", {}).get(event, 0))
                for event in spec.get("events", [])
            )
            if total < int(spec["count"]): return False
        if "consecutive_choice_rounds" in trigger:
            streak = self.s.stats.get("choice_round_streak", {})
            start = int(baseline.get("choice_round_start", max(1, int(baseline.get("spin", self.s.spin)))))
            since_acquisition = max(0, int(streak.get("last_spin", -1)) - start + 1)
            if streak.get("last_spin") != self.s.spin or min(int(streak.get("length", 0)), since_acquisition) < int(trigger["consecutive_choice_rounds"]):
                return False
        if "choice_event_count" in trigger:
            if not self.s.pending:
                return False
            current_choice = self.s.pending[0]
            spec = trigger["choice_event_count"]
            current_count = int(current_choice.details.get("event_counts", {}).get(spec["event"], 0))
            if current_choice.details.get("choice_uid") == baseline.get("choice_uid"):
                current_count -= int(baseline.get("choice_events", {}).get(spec["event"], 0))
            if current_count < int(spec["count"]):
                return False
        if "event_count_round" in trigger:
            spec=trigger["event_count_round"]
            # A newly acquired essence starts counting from the current
            # in-round event total. At the next spin the counter resets, so a
            # snapshot from an earlier round must not carry over.
            round_baseline = baseline.get("round_events", {})
            if int(baseline.get("spin", self.s.spin)) != self.s.spin:
                round_baseline = {}
            current = self._round_events.get(spec["event"], 0) - int(round_baseline.get(spec["event"], 0))
            if current < int(spec["count"]): return False
        if "event_value" in trigger:
            spec=trigger["event_value"]; event=spec["event"]
            if int(values.get(event,0))-int(baseline.get("values",{}).get(event,0)) < int(spec["value"]): return False
        if "round_events" in trigger and not all(self._round_events.get(x,0)>0 for x in trigger["round_events"]): return False
        if "board_tag_count" in trigger:
            spec=trigger["board_tag_count"]
            if sum(1 for d in board_defs if spec["tag"] in d.get("tags",[])) < int(spec["count"]): return False
        if "board_has_all_ids" in trigger:
            present_ids = {definition["id"] for definition in board_defs}
            if not set(trigger["board_has_all_ids"]).issubset(present_ids): return False
        if "board_filter_count" in trigger:
            spec=trigger["board_filter_count"]; tags=set(spec.get("tags",[]))
            found=sum(1 for d in board_defs if (not tags or tags.intersection(d.get("tags",[]))) and (not spec.get("rarity") or int(d.get("rarity",0))==int(spec["rarity"])))
            if found < int(spec["count"]): return False
        if "board_base_zero" in trigger and sum(1 for d in board_defs if int(d.get("base",0))==0) < int(trigger["board_base_zero"]): return False
        if "board_same_count" in trigger and max(Counter(d["id"] for d in board_defs).values(), default=0) < int(trigger["board_same_count"]): return False
        if trigger.get("board_all_unique") and len(board_defs) != len({d["id"] for d in board_defs}): return False
        if "board_adjacent_same" in trigger and not self._has_adjacent_same(int(trigger["board_adjacent_same"])): return False
        if "board_empty_slots" in trigger:
            capacity = self.board_capacity()
            occupied = sum(1 for instance in self._board if self._present(instance))
            if capacity - occupied < int(trigger["board_empty_slots"]): return False
        income_conditions = {"income_max", "income_even", "income_multiple"}
        if income_conditions.intersection(trigger) and "last_income" not in self.s.stats:
            # No observed settlement is not an observed zero-income turn.
            # Legacy saves lacking this field wait for a real spin; genuine
            # zero income remains legal for parity/multiple/max predicates.
            return False
        income=int(self.s.stats.get("last_income",0))
        if "income_max" in trigger and income > int(trigger["income_max"]): return False
        if trigger.get("income_even") and income % 2: return False
        if "income_multiple" in trigger and income % int(trigger["income_multiple"]): return False
        if "token_count" in trigger:
            spec=trigger["token_count"]
            if int(self.s.tokens.get(spec["token"],0)) < int(spec["count"]): return False
        if "pool_size" in trigger and len(self.s.ingredients) < int(trigger["pool_size"]): return False
        if "essence_count" in trigger and len(self.s.essences) < int(trigger["essence_count"]): return False
        if "choice_offers" in trigger and (not self.s.pending or len(self.s.pending[0].offers) < int(trigger["choice_offers"])): return False
        if "choice_contains_rarities" in trigger:
            if not set(trigger["choice_contains_rarities"]).issubset(set(self.s.stats.get("last_choice_rarities",[]))): return False
        if "owned_item_prefix" in trigger:
            spec=trigger["owned_item_prefix"]
            if sum(1 for x in self.s.items if x.endswith("_reagent")) < int(spec["count"]): return False
        return bool(trigger)

    def _apply_essence_effect(self, effect: dict[str, Any], source: str, *, context: dict[str, Any] | None = None) -> None:
        context = context or {}
        if effect.get("gold"): self._gain_gold(int(effect["gold"]), source)
        if effect.get("gold_percent_of_stat"):
            spec = effect["gold_percent_of_stat"]
            amount = int(float(self.s.stats.get(spec["stat"], 0)) * float(spec["percent"]))
            if amount: self._gain_gold(amount, source)
        for token, amount in effect.get("tokens", {}).items(): self._gain_token(token, int(amount), source)
        if effect.get("rarity_multiplier"): self.s.rarity_multiplier *= float(effect["rarity_multiplier"])
        if effect.get("flag"): self.s.flags.update(effect["flag"])
        if self.s.flags.pop("cancel_order_penalty", False):
            self.s.flags.pop("next_order_penalty", None)
        for flag, amount in effect.get("increment_flags", {}).items():
            self.s.flags[flag] = int(self.s.flags.get(flag, 0)) + int(amount)
        if effect.get("item_storage"):
            spec = effect["item_storage"]
            self._store_item_gold(str(spec["item_id"]), int(spec["amount"]), source)
        if effect.get("context_permanent_bonus") and context.get("instance") is not None:
            self._permanent_bonus(context["instance"], int(effect["context_permanent_bonus"]))
        if effect.get("choices"):
            spec = effect["choices"]
            for _ in range(int(spec.get("groups", 1))):
                self.s.pending.append(self.make_choice(
                    spec.get("kind", "ingredient"),
                    count=int(spec.get("candidates", 3)),
                    source="essence",
                    tag_filter=spec.get("tag"),
                ))
        if effect.get("disable_ingredient_generation"):
            self.s.flags["ingredient_generation_permanently_disabled"] = True
            amount = int(effect.get("generation_bonus", 0))
            if amount:
                self.s.flags["ingredient_generation_bonus"] = int(
                    self.s.flags.get("ingredient_generation_bonus", 0)
                ) + amount
            for instance in self.s.ingredients:
                self._apply_generation_bonus_to_instance(instance)
        if effect.get("permanent_bonus"):
            spec=effect["permanent_bonus"]
            target_ids: set[str] | None = None
            if spec.get("context_id") and context.get("instance") is not None:
                target_ids = {str(context["instance"].def_id)}
            if spec.get("persistent") and target_ids:
                global_bonuses = self.s.flags.setdefault("global_permanent_bonuses", {})
                for target_id in target_ids:
                    global_bonuses[target_id] = int(global_bonuses.get(target_id, 0)) + int(spec.get("amount", 0))
            for inst in self.s.ingredients:
                definition=self.catalog.ingredients[inst.def_id]; tags=set(definition.get("tags",[]))
                matches = bool(target_ids and definition["id"] in target_ids) or definition["id"] in spec.get("ids", []) or spec.get("tag") in tags or bool(tags.intersection(spec.get("tags",[]))) or ("base" in spec and int(definition.get("base",0))==int(spec["base"]))
                if spec.get("rarity") and int(definition.get("rarity",0))!=int(spec["rarity"]): matches=False
                if matches and not spec.get("persistent"):
                    self._permanent_bonus(inst, int(spec["amount"]))
        if effect.get("random_permanent_bonus") and self.s.ingredients: self._permanent_bonus(self.r.choice(self.s.ingredients),int(effect["random_permanent_bonus"]))
        if effect.get("add_ingredient"):
            spec=effect["add_ingredient"]
            for _ in range(int(spec.get("count", 1))):
                if spec.get("id"):
                    self.add_ingredient(str(spec["id"]))
                else:
                    rarity=int(spec.get("rarity") or self.roll_rarity("ingredient"))
                    self.add_ingredient(self._draw_definition("ingredient",rarity,tag=spec.get("tag")))
        if effect.get("fixed_choice"):
            spec=effect["fixed_choice"]; self.s.pending.append(self.make_choice(spec["kind"],fixed_rarity=int(spec["rarity"]),source="essence"))
        if effect.get("copy_recent") and self.s.stats.get("recent_copied"): self.add_ingredient(self.s.stats["recent_copied"])
        if effect.get("remove_random_rarity"):
            spec=effect["remove_random_rarity"]; candidates=[x for x in self.s.ingredients if int(self.catalog.ingredients[x.def_id].get("rarity",0))==int(spec["rarity"])]
            for inst in self.r.sample(candidates,min(len(candidates),int(spec["count"]))): self._remove(inst,"essence",None)
        if effect.get("highest_removed_to_random") and self._removed_values and self.s.ingredients:
            self._permanent_bonus(self.r.choice(self.s.ingredients),max(v for _,v in self._removed_values))
        if effect.get("advance_random_essences"):
            others=[x for x in self.s.essences if self.catalog.essences[x]["name"] != source]
            for essence_id in self.r.sample(others,min(len(others),int(effect["advance_random_essences"]))): self.s.stats.setdefault("essence_hits",{})[essence_id]=1

    def status_payload(self) -> dict[str, Any]:
        amount, total_spins = self.current_order()
        choice = self.s.pending[0] if self.s.pending else None
        return {
            "status": self.s.status, "seed": self.s.seed, "difficulty": self.s.difficulty,
            "fun_mode": self.s.fun_mode,
            "gold": self.s.gold, "spin": self.s.spin,
            "order": self.s.order_index + 1, "order_amount": amount,
            "spins_left": self.s.spins_left, "order_spins": total_spins,
            "pool_size": len(self.s.ingredients), "board_capacity": self.board_capacity(),
            "tokens": dict(self.s.tokens), "items": list(self.s.items), "essences": list(self.s.essences),
            "pending": None if not choice else {"kind":choice.kind,"offers":list(choice.offers),"can_skip":choice.can_skip,"source":choice.source,"tag_filter":choice.tag_filter,"details":dict(choice.details)},
            "ingredient_generation_disabled": self.ingredient_generation_disabled(),
            "ingredient_generation_permanently_disabled": bool(self.s.flags.get("ingredient_generation_permanently_disabled", False)),
            "ingredient_generation_bonus": int(self.s.flags.get("ingredient_generation_bonus", 0)),
            "awaiting_mode_choice": bool(choice and choice.kind == "run_end"),
            "endless_mode": bool(self.s.endless_mode),
            "endless_order": int(self.s.endless_order),
            "endless_target": int(self.s.endless_target),
            "peace_mode": bool(self.s.peace_mode),
            "peace_order": int(self.s.peace_order),
            "peace_target": PEACE_MODE_TARGET,
            "highest_endless_order": int(self.s.stats.get("highest_endless_order", 0)),
            "endless_orders_completed": int(self.s.stats.get("endless_orders_completed", 0)),
            "highest_endless_single_turn_gold": int(self.s.stats.get("highest_endless_single_turn_gold", 0)),
            "highest_single_turn_gold": int(self.s.stats.get("highest_single_turn_gold", 0)),
            "last_board": list(self.s.last_board), "last_log": list(self.s.last_log),
        }

    def get_view_state(self) -> dict[str, Any]:
        """Return the complete machine-readable state used by UI adapters.

        This is an alias-level API over the established Agent payload.  It
        keeps the desktop layer from inventing a second authoritative state
        representation while leaving the CLI/Agent protocol unchanged.
        """

        return self.agent_payload("view")

    def _definition_view(self, kind: str, def_id: str) -> dict[str, Any]:
        """Return a JSON-safe copy of a catalog definition for agent clients."""
        if kind == "run_end":
            if def_id not in RUN_END_OPTIONS:
                raise GameError(f"unknown run-end option: {def_id}")
            return dict(RUN_END_OPTIONS[def_id])
        if kind == "ingredient":
            definition = self.catalog.ingredients[def_id]
        elif kind == "item":
            definition = self.catalog.items[def_id]
        elif kind == "essence":
            definition = self.catalog.essences[def_id]
        else:
            raise GameError(f"unknown catalog kind: {kind}")
        return dict(definition)

    def agent_available_actions(self) -> list[str]:
        """Return canonical one-step commands currently accepted by the engine.

        The strings are intentionally executable command forms so an agent can
        select one without having to infer positional arguments from another
        field.  Read-only actions remain available after a run has ended.
        """
        actions = ["status", "inventory", "help"]
        if self.s.status != "playing":
            return actions
        if self.s.pending:
            choice = self.s.pending[0]
            actions.extend(f"choose {index}" for index in range(1, len(choice.offers) + 1))
            if choice.can_skip:
                actions.append("skip")
            if choice.offers and choice.kind not in {"essence", "run_end", "bundle"} and self.s.tokens.get("roll", 0) > 0:
                actions.append("reroll")
            return actions
        actions.append("spin")
        if self.s.tokens.get("remove", 0) > 0:
            for index, instance in enumerate(self.s.ingredients, 1):
                definition = self.catalog.ingredients[instance.def_id]
                if definition.get("removable", True):
                    actions.append(f"remove {index}")
        for item_id in self.s.items:
            if self.catalog.items[item_id].get("toggle_flag"):
                actions.append(f"toggle {item_id}")
            active = self.catalog.items[item_id].get("active")
            if not active:
                continue
            if active.get("order_book") and self.s.spins_left != 1:
                continue
            actions.append(f"use {item_id}")
        return actions

    def agent_action_specs(self) -> list[dict[str, Any]]:
        """Return structured equivalents of :meth:`agent_available_actions`."""
        specs: list[dict[str, Any]] = [
            {"action": "status"},
            {"action": "inventory"},
            {"action": "help"},
        ]
        if self.s.status != "playing":
            return specs
        if self.s.pending:
            choice = self.s.pending[0]
            for index, def_id in enumerate(choice.offers, 1):
                specs.append({"action": "choose", "index": index, "id": def_id})
            if choice.can_skip:
                specs.append({"action": "skip"})
            if choice.offers and choice.kind not in {"essence", "run_end", "bundle"} and self.s.tokens.get("roll", 0) > 0:
                specs.append({"action": "reroll"})
            return specs
        specs.append({"action": "spin"})
        if self.s.tokens.get("remove", 0) > 0:
            for index, instance in enumerate(self.s.ingredients, 1):
                definition = self.catalog.ingredients[instance.def_id]
                if definition.get("removable", True):
                    specs.append({"action": "remove", "index": index, "id": instance.def_id})
        for item_id in self.s.items:
            if self.catalog.items[item_id].get("toggle_flag"):
                specs.append({"action": "toggle", "item_id": item_id, "enabled": self.ingredient_generation_disabled()})
            active = self.catalog.items[item_id].get("active")
            if active and (not active.get("order_book") or self.s.spins_left == 1):
                specs.append({"action": "use", "item_id": item_id})
        return specs

    def agent_payload(
        self,
        action: str,
        *,
        ok: bool = True,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the stable machine-readable state envelope.

        ``state`` is the complete persisted game state.  The additional view
        fields provide catalog metadata and queue details needed to make the
        next decision without loading project data separately.
        """
        payload = self.status_payload()
        state = self.s
        amount, total_spins = self.current_order()

        ingredients: list[dict[str, Any]] = []
        for slot, instance in enumerate(state.ingredients, 1):
            row = {
                "slot": slot,
                "uid": instance.uid,
                "id": instance.def_id,
                "permanent_bonus": instance.permanent_bonus,
                "age": instance.age,
                "counter": instance.counter,
                "stored_gold": instance.stored_gold,
                "flags": dict(instance.flags),
                "definition": self._definition_view("ingredient", instance.def_id),
            }
            ingredients.append(row)

        items = [self._definition_view("item", item_id) for item_id in state.items]
        essences = [self._definition_view("essence", essence_id) for essence_id in state.essences]
        pending_choices: list[dict[str, Any]] = []
        for queue_index, choice in enumerate(state.pending):
            kind = choice.kind
            offers = [
                {
                    "index": index,
                    "id": def_id,
                    "definition": (
                        dict(choice.details.get("options", {}).get(def_id, {}))
                        if kind == "bundle"
                        else self._definition_view(kind, def_id)
                    ),
                }
                for index, def_id in enumerate(choice.offers, 1)
            ]
            pending_choices.append(
                {
                    "queue_index": queue_index,
                    "kind": kind,
                    "source": choice.source,
                    "can_skip": choice.can_skip,
                    "tag_filter": choice.tag_filter,
                    "offers": offers,
                }
            )

        payload.update(
            {
                "protocol": "crucible-echoes-agent/v1",
                "ok": ok,
                "action": action,
                "error": error,
                "state": state.to_dict(),
                "order_detail": {
                    "number": state.order_index + 1,
                    "completed": state.order_index,
                    "amount": amount,
                    "spins_left": state.spins_left,
                    "spins_total": total_spins,
                    "endless_mode": bool(state.endless_mode),
                    "endless_order": int(state.endless_order),
                    "endless_target": int(state.endless_target),
                    "peace_mode": bool(state.peace_mode),
                    "peace_order": int(state.peace_order),
                    "peace_target": PEACE_MODE_TARGET,
                },
                "ingredients": ingredients,
                "items_detail": items,
                "essences_detail": essences,
                "pending_choices": pending_choices,
                "consumed_essences": list(state.consumed_essences),
                "removed_history": list(state.removed_history),
                "acquired_once": list(state.acquired_once),
                "expanded": state.expanded,
                "rarity_multiplier": state.rarity_multiplier,
                "flags": dict(state.flags),
                "stats": state.stats,
                "spawn_counters": dict(state.stats.setdefault("spawn_counters", {})),
                "last_board": list(state.last_board),
                "last_log": list(state.last_log),
                "available_actions": self.agent_available_actions(),
                "available_action_specs": self.agent_action_specs(),
            }
        )
        return payload
