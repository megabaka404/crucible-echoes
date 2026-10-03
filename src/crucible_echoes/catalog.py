from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Any


# Only known ingredient-reference fields. In particular bundle option IDs,
# event names, counters, and tags are not ingredient IDs.
_INGREDIENT_REFERENCE_PATHS = {
    "ingredient": (
        "transform_after.into", "chance_transform.into", "chance_transforms.*.into",
        "periodic_spawn.id", "chance_spawn.id", "spawn_each_spin.id",
        "periodic_spawn.exclude.*", "chance_spawn.exclude.*", "spawn_each_spin.exclude.*",
        "aura.id", "aura.ids.*", "board_presence_gold.ids.*", "on_removed.exclude.*",
    ),
    "item": (
        "bonuses.*.id", "bonuses.*.ids.*", "protect.id", "protect.ids.*",
        "protect_once.id", "protect_once.ids.*", "active.add_ingredients.id",
        "on_acquire.add_ingredient.id", "on_acquire.add_ingredients.id",
        "on_acquire.bundle_choice.options.*.add_ingredients.*",
    ),
    "essence": (
        "effect.add_ingredient.id", "effect.permanent_bonus.id", "effect.permanent_bonus.ids.*",
    ),
}


@dataclass(frozen=True)
class Catalog:
    ingredients: dict[str, dict[str, Any]]
    items: dict[str, dict[str, Any]]
    essences: dict[str, dict[str, Any]]
    progression: dict[str, Any]

    @classmethod
    def load(cls) -> "Catalog":
        root = files("crucible_echoes").joinpath("data")

        def read(name: str) -> Any:
            return json.loads(root.joinpath(name).read_text(encoding="utf-8"))

        ingredient_rows = read("ingredients.json")
        item_rows = read("items.json")
        essence_rows = read("essences.json")
        def indexed(rows: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
            result: dict[str, dict[str, Any]] = {}
            for row in rows:
                key = row["id"]
                if key in result:
                    raise ValueError(f"Duplicate {label} ID: {key}")
                result[key] = row
            return result
        return cls(
            ingredients=indexed(ingredient_rows, "ingredient"),
            items=indexed(item_rows, "item"),
            essences=indexed(essence_rows, "essence"),
            progression=read("progression.json"),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        for label, collection in (("ingredient", self.ingredients), ("item", self.items), ("essence", self.essences)):
            for key, row in collection.items():
                if row.get("id") != key:
                    errors.append(f"{label} key mismatch: {key}")
                if not row.get("name"):
                    errors.append(f"{label} lacks name: {key}")
                if label != "essence":
                    rarity = row.get("rarity")
                    if isinstance(rarity, bool) or not isinstance(rarity, int):
                        errors.append(f"{label} {key} rarity must be an integer")
                    elif not (1 <= rarity <= 4) and not (label == "ingredient" and rarity == 0 and row.get("offerable") is False):
                        errors.append(f"{label} {key} rarity must be 1..4 (non-offerable system ingredients may use 0)")
                for reference_path in _INGREDIENT_REFERENCE_PATHS[label]:
                    def inspect(value: Any, remaining: list[str], path: str) -> None:
                        if not remaining:
                            if not isinstance(value, str) or value not in self.ingredients:
                                errors.append(f"{label} {key} {path} references missing ingredient {value!r}")
                            return
                        field, *rest = remaining
                        if field == "*":
                            if not isinstance(value, list):
                                errors.append(f"{label} {key} {path} must be a list")
                                return
                            for index, child in enumerate(value):
                                inspect(child, rest, f"{path}[{index}]")
                        elif isinstance(value, dict) and field in value:
                            inspect(value[field], rest, f"{path}.{field}" if path else field)
                    inspect(row, reference_path.split("."), "")
        for essence in self.essences.values():
            item_id = essence.get("item_id")
            if item_id and item_id not in self.items:
                errors.append(f"essence {essence['id']} references missing item {item_id}")
        return errors
