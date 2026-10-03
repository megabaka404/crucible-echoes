"""Transform the existing Agent payload into a desktop-friendly view model.

The desktop client deliberately receives a complete snapshot after each
action.  It never calculates game legality or values itself; those remain the
responsibility of :class:`crucible_echoes.engine.GameEngine`.
"""

from __future__ import annotations

from typing import Any

from crucible_echoes.engine import GameEngine
from crucible_echoes.geometry import adjacent_indices, board_coords, orthogonal_indices


def _definition(engine: GameEngine, kind: str, def_id: str) -> dict[str, Any]:
    collection = {
        "ingredient": engine.catalog.ingredients,
        "item": engine.catalog.items,
        "essence": engine.catalog.essences,
    }[kind]
    return dict(collection.get(def_id, {"id": def_id, "name": def_id, "description": ""}))


def _codex(engine: GameEngine, payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Return definitions the player has encountered or currently owns."""

    state = engine.s
    history = state.stats.get("observed_content", {})
    seen_ingredients = set(history.get("ingredients", [])) | set(state.stats.get("seen_types", []))
    seen_ingredients.update(instance.def_id for instance in state.ingredients)
    seen_ingredients.update(row.get("id") for row in payload.get("ingredients", []))
    for choice in payload.get("pending_choices", []):
        if choice.get("kind") == "ingredient":
            seen_ingredients.update(row.get("id") for row in choice.get("offers", []))

    seen_items = set(history.get("items", [])) | set(state.items)
    seen_essences = set(history.get("essences", [])) | set(state.essences) | set(state.consumed_essences)
    for choice in payload.get("pending_choices", []):
        if choice.get("kind") == "item":
            seen_items.update(row.get("id") for row in choice.get("offers", []))
        elif choice.get("kind") == "essence":
            seen_essences.update(row.get("id") for row in choice.get("offers", []))

    return {
        "ingredients": [
            _definition(engine, "ingredient", def_id)
            for def_id in sorted(seen_ingredients)
            if def_id in engine.catalog.ingredients
        ],
        "items": [
            _definition(engine, "item", def_id)
            for def_id in sorted(seen_items)
            if def_id in engine.catalog.items
        ],
        "essences": [
            _definition(engine, "essence", def_id)
            for def_id in sorted(seen_essences)
            if def_id in engine.catalog.essences
        ],
    }


def _board_view(engine: GameEngine, payload: dict[str, Any]) -> list[dict[str, Any]]:
    instances = {row["uid"]: row for row in payload.get("ingredients", [])}
    result: list[dict[str, Any]] = []
    board = list(payload.get("last_board", []))
    # Before the first spin the core has no evaluated ``last_board`` yet, but
    # the player still needs to see the five starting ingredients (and be able
    # to choose a Delete target later).  Render the current pool in the normal
    # board order until the first evaluated board exists.  Once a spin has
    # happened, keep using the real sampled board so adjacency/value display
    # remains authoritative and deterministic.
    if not board and payload.get("ingredients"):
        coords = board_coords(engine.s.expanded, engine.s.fun_mode)
        for index, row in enumerate(payload.get("ingredients", [])):
            if index >= len(coords):
                break
            definition = row.get("definition", {})
            board.append(
                {
                    "uid": row.get("uid"),
                    "id": row.get("id"),
                    "coord": list(coords[index]),
                    "present": True,
                    # No evaluated round value exists before the first spin;
                    # show the stable base/permanent component value instead.
                    "value": int(row.get("stable_value", 0)),
                }
            )
    coordinates = [tuple(row.get("coord", (0, index))) for index, row in enumerate(board)]
    matches_settled_board = (
        bool(payload.get("last_board"))
        and len(board) == len(engine._board) == len(engine._coords)
        and all(
            str(row.get("uid")) == str(instance.uid) and coordinates[index] == engine._coords[index]
            for index, (row, instance) in enumerate(zip(board, engine._board))
        )
    )
    if matches_settled_board:
        # The core restores the topology that actually settled this board,
        # including one-spin flags already decayed and panorama corners.
        neighbor_indices = {index: engine._neighbors(index) for index in range(len(board))}
    elif engine.s.flags.get("all_adjacent_spins"):
        neighbor_indices = {
            index: [other for other in range(len(board)) if other != index]
            for index in range(len(board))
        }
    elif engine.s.fun_mode in {"giant", "minimal"}:
        neighbor_indices = {
            index: orthogonal_indices(coordinates, index)
            for index in range(len(board))
        }
    else:
        neighbor_indices = {
            index: adjacent_indices(coordinates, index)
            for index in range(len(board))
        }

    for index, row in enumerate(board):
        instance = instances.get(row.get("uid"), {})
        definition = _definition(engine, "ingredient", row.get("id", ""))
        result.append(
            {
                **row,
                "present": bool(row.get("present", True)) and row.get("uid") in instances,
                "uid": row.get("uid"),
                "pool_slot": instance.get("slot"),
                "name": row.get("name", definition.get("name", row.get("id", ""))),
                "rarity": definition.get("rarity", 0),
                "base": definition.get("base", 0),
                "tags": list(definition.get("tags", [])),
                "description": definition.get("description", ""),
                "permanent_bonus": instance.get("permanent_bonus", 0),
                "stable_value": instance.get("stable_value"),
                "value_kind": "settled" if payload.get("last_board") else "stable",
                "age": instance.get("age", 0),
                "counter": instance.get("counter", 0),
                "stored_gold": instance.get("stored_gold", 0),
                "flags": dict(instance.get("flags", {})),
                "neighbors": [
                    target for target in neighbor_indices.get(index, [])
                    if board[target].get("uid") in instances and board[target].get("present", True)
                ],
            }
        )
    return result


def build_view_state(engine: GameEngine, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build one complete JSON-safe GUI snapshot from the core payload."""

    payload = dict(payload or engine.agent_payload("view"))
    live_instances = {instance.uid: instance for instance in engine.s.ingredients}
    payload["ingredients"] = [
        {
            **row,
            "stable_value": engine.stable_ingredient_value(live_instances[row["uid"]]),
        }
        for row in payload.get("ingredients", [])
    ]
    board = _board_view(engine, payload)
    amount = int(payload.get("order_amount", 0))
    # The initial pool layout is informational; no round has been evaluated
    # until the core supplies ``last_board`` after the first spin.
    current_value = (
        sum(int(row.get("value", 0)) for row in board if row.get("present", True))
        if payload.get("last_board")
        else 0
    )
    # Grid dimensions describe the full laboratory, not only occupied slots.
    # An initial pool of three ingredients still occupies a 3x4 minimal board.
    coords = board_coords(engine.s.expanded, engine.s.fun_mode)
    max_row = max((coord[0] for coord in coords), default=3)
    max_col = max((coord[1] for coord in coords), default=4)

    return {
        "protocol": "crucible-echoes-desktop/v1",
        "ok": payload.get("ok", True),
        "error": payload.get("error"),
        "status": payload.get("status"),
        "seed": payload.get("seed"),
        "seed_text": str(engine.s.seed),
        "difficulty": payload.get("difficulty"),
        "fun_mode": payload.get("fun_mode", "none"),
        "gold": payload.get("gold", 0),
        "current_value": current_value,
        "spin": payload.get("spin", 0),
        "order": payload.get("order", 1),
        "order_amount": amount,
        "spins_left": payload.get("spins_left", 0),
        "order_spins": payload.get("order_spins", 0),
        "order_progress": (
            float(payload.get("gold", 0)) / int(payload.get("peace_target", 1_000_000))
            if payload.get("peace_mode")
            else float(payload.get("gold", 0)) / amount if amount > 0 else 1.0
        ),
        "pool_size": payload.get("pool_size", 0),
        "board_capacity": payload.get("board_capacity", 20),
        "board_columns": max_col + 1,
        "board_rows": max_row + 1,
        "board_row_offset": 2 if engine.s.expanded else 1,
        "expanded": engine.s.expanded,
        "tokens": dict(payload.get("tokens", {})),
        "ingredients": list(payload.get("ingredients", [])),
        "items": list(payload.get("items_detail", [])),
        "essences": list(payload.get("essences_detail", [])),
        "consumed_essences": list(payload.get("consumed_essences", [])),
        "board": board,
        "last_board": board,
        "pending_choices": list(payload.get("pending_choices", [])),
        "available_actions": list(payload.get("available_actions", [])),
        "available_action_specs": list(payload.get("available_action_specs", [])),
        "ingredient_generation_disabled": payload.get("ingredient_generation_disabled", False),
        "ingredient_generation_permanently_disabled": payload.get(
            "ingredient_generation_permanently_disabled", False
        ),
        "endless_mode": payload.get("endless_mode", False),
        "endless_order": payload.get("endless_order", 0),
        "endless_target": payload.get("endless_target", 0),
        "peace_mode": payload.get("peace_mode", False),
        "peace_order": payload.get("peace_order", 0),
        "peace_target": payload.get("peace_target", 1_000_000),
        "awaiting_mode_choice": payload.get("awaiting_mode_choice", False),
        "last_log": list(payload.get("last_log", [])),
        "stats": dict(payload.get("stats", {})),
        "codex": _codex(engine, payload),
        "ui": {
            "board_columns": max_col + 1,
            "board_rows": max_row + 1,
            "capacity": payload.get("board_capacity", 20),
        },
    }
