from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class IngredientInstance:
    uid: int
    def_id: str
    permanent_bonus: int = 0
    age: int = 0
    counter: int = 0
    stored_gold: int = 0
    flags: dict[str, Any] = field(default_factory=dict)
    # ``mutation`` entertainment mode keeps this per-instance counter.  It is
    # a dataclass field (rather than a process-local map) so stateless Agent
    # actions and JSON saves preserve it naturally.  Older saves omit it and
    # receive the default zero during deserialization.
    mutation_draw_count: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IngredientInstance":
        return cls(**data)


@dataclass
class PendingChoice:
    kind: str
    offers: list[str]
    can_skip: bool = True
    source: str = "spin"
    minimum_rarity: int | None = None
    tag_filter: str | None = None
    # Optional data for non-standard but still data-driven choices (for
    # example an all-or-nothing ingredient bundle).  Older saves simply omit
    # this field and get an empty mapping.
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingChoice":
        return cls(**data)


@dataclass
class GameState:
    version: int
    seed: int
    rng_state: int
    difficulty: int
    gold: int
    status: str
    spin: int
    order_index: int
    spins_left: int
    next_uid: int
    ingredients: list[IngredientInstance] = field(default_factory=list)
    items: list[str] = field(default_factory=list)
    essences: list[str] = field(default_factory=list)
    consumed_essences: list[str] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=lambda: {"roll": 0, "remove": 0, "essence": 0})
    pending: list[PendingChoice] = field(default_factory=list)
    removed_history: list[str] = field(default_factory=list)
    acquired_once: list[str] = field(default_factory=list)
    expanded: bool = False
    rarity_multiplier: float = 1.0
    last_board: list[dict[str, Any]] = field(default_factory=list)
    last_log: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    flags: dict[str, Any] = field(default_factory=dict)
    # Post-mainline infinite mode is opt-in.  Defaults keep legacy saves valid.
    endless_mode: bool = False
    endless_order: int = 0
    endless_target: int = 0
    peace_mode: bool = False
    peace_order: int = 0
    # Entertainment modes are orthogonal to difficulty and mutually
    # exclusive.  Appending the field keeps positional/legacy JSON loading
    # compatible while ``from_dict`` supplies ``none`` to old saves.
    fun_mode: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GameState":
        copied = dict(data)
        copied["ingredients"] = [IngredientInstance.from_dict(x) for x in copied.get("ingredients", [])]
        copied["pending"] = [PendingChoice.from_dict(x) for x in copied.get("pending", [])]
        stats = dict(copied.get("stats") or {})
        stats.setdefault("spawn_counters", {})
        stats.setdefault("round_events", {})
        stats.setdefault("item_event_counts", {})
        stats.setdefault("item_trigger_counts", {})
        stats.setdefault("item_storage", {})
        stats.setdefault("endless_orders_completed", 0)
        stats.setdefault("highest_endless_order", 0)
        stats.setdefault("highest_endless_single_turn_gold", 0)
        stats.setdefault("highest_single_turn_gold", 0)
        copied["stats"] = stats
        copied.setdefault("endless_mode", False)
        copied.setdefault("endless_order", 0)
        copied.setdefault("endless_target", 0)
        copied.setdefault("peace_mode", False)
        copied.setdefault("peace_order", 0)
        copied.setdefault("fun_mode", "none")
        return cls(**copied)
