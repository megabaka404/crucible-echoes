"""Deep, report-only balance diagnostics for Crucible Echoes.

This module intentionally does not alter live balance data.  It reuses the
normal deterministic engine and can run smaller focused samples in addition
to the official difficulty sweep.
"""

from __future__ import annotations

import argparse
import copy
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from .catalog import Catalog
from .engine import GameEngine
from .model import GameState, PendingChoice
from .simulation import HeuristicStrategy, GameRecord, derive_seed, run_batch, simulate_game


FOCUS_INGREDIENTS = (
    "lucky_potion", "paper", "upgrade_magic", "catnip", "rust", "coin",
    "herb", "flame", "wood_key", "wood_chest",
)
FOCUS_ITEMS = (
    "magic_filter", "animal_registry", "impossible_container", "large_reactor",
    "lucky_charm", "lucky_compass", "ore_sorting_table", "small_safe",
    "scrap_bin", "recycling_permit",
)
BUFFS = {
    "lucky_potion": ("ingredients", "lucky_potion", {"potion": {"flag": "lucky_choice"}}),
    "paper": ("ingredients", "paper", {"growth_chance": 0.15}),
    "magic_filter": ("items", "magic_filter", {"negative_cancel_chance": 0.5}),
    "animal_registry": ("items", "animal_registry", {"first_animal_gold": 6}),
    "large_reactor": ("items", "large_reactor", {"on_acquire": {"fixed_ingredient_choices": [1, 1, 2]}}),
    "lucky_charm": ("items", "lucky_charm", {"rarity_multiplier": 1.05}),
    "lucky_compass": ("items", "lucky_compass", {"candidate_rarity_weight": 1.20}),
    "small_safe": ("items", "small_safe", {"event_bonus": {"token": 2}}),
}


class RejectItemStrategy(HeuristicStrategy):
    """Counterfactual policy that refuses one named item when possible."""

    def __init__(self, rejected_id: str):
        self.rejected_id = rejected_id
        self.name = f"heuristic-v1-reject-{rejected_id}"

    def choose(self, engine: GameEngine, choice: PendingChoice) -> int | None:
        if choice.kind == "item" and self.rejected_id in choice.offers and choice.can_skip:
            return None
        return super().choose(engine, choice)


class MineralFlowStrategy(HeuristicStrategy):
    """Generic tag-driven strategy used only for the ore-table stress sample."""

    def score_components(self, engine: GameEngine, kind: str, def_id: str) -> dict[str, float]:
        components = super().score_components(engine, kind, def_id)
        if kind == "ingredient":
            tags = set(engine.catalog.ingredients[def_id].get("tags", []))
            if tags.intersection({"stone", "ore", "metal"}):
                components["synergy"] += 12.0
        return components


def _ore_flow_ab(seed: int, games: int, catalog: Catalog) -> dict[str, Any]:
    """Run equal mineral-focused samples with and without the sorting table."""

    def run(with_table: bool) -> dict[str, Any]:
        totals = {"income": 0, "spins": 0, "mineral_rarity": [], "generated_minerals": [],
                  "final_pool": [], "max_pool": [], "wins": 0}
        for index in range(games):
            seen_uids: set[int] = set()
            generated_count = 0
            generated_rarities: list[int] = []

            def on_start(engine: GameEngine) -> None:
                if with_table:
                    engine.s.items.append("ore_sorting_table")
                seen_uids.update(instance.uid for instance in engine.s.ingredients)

            def on_spin(engine: GameEngine) -> None:
                nonlocal generated_count
                current = {instance.uid: instance for instance in engine.s.ingredients}
                for uid, instance in current.items():
                    if uid in seen_uids:
                        continue
                    definition = engine.catalog.ingredients[instance.def_id]
                    if set(definition.get("tags", [])).intersection({"stone", "ore", "metal"}):
                        generated_count += 1
                        generated_rarities.append(int(definition.get("rarity", 1)))
                seen_uids.update(current)
                totals["income"] += int(engine.s.stats.get("last_income", 0))
                totals["spins"] += 1

            def on_choice(engine: GameEngine, _choice: PendingChoice, _selected: str | None) -> None:
                seen_uids.update(instance.uid for instance in engine.s.ingredients)

            record = simulate_game(
                seed=derive_seed(seed, index), difficulty=1, strategy=MineralFlowStrategy(),
                game_index=index, on_start=on_start, on_spin=on_spin, on_choice=on_choice,
                catalog=catalog,
            )
            totals["generated_minerals"].append(generated_count)
            totals["mineral_rarity"].extend(generated_rarities)
            totals["final_pool"].append(len(record.held_ingredients) + len(record.held_equipment))
            totals["max_pool"].append(int(record.final_attributes.get("max_pool_size", 0)))
            totals["wins"] += int(record.won)
        return {
            "games": games,
            "win_rate": totals["wins"] / games if games else 0.0,
            "average_gold_per_spin": totals["income"] / totals["spins"] if totals["spins"] else 0.0,
            "average_mineral_rarity": mean(totals["mineral_rarity"]) if totals["mineral_rarity"] else None,
            "average_generated_minerals": mean(totals["generated_minerals"]) if totals["generated_minerals"] else 0.0,
            "average_final_pool": mean(totals["final_pool"]) if totals["final_pool"] else 0.0,
            "average_max_pool": mean(totals["max_pool"]) if totals["max_pool"] else 0.0,
            "average_spins": totals["spins"] / games if games else 0.0,
        }

    without_table = run(False)
    with_table = run(True)
    return {
        "seed": seed,
        "games_per_side": games,
        "strategy": "mineral-flow-v1 (generic stone/ore/metal tags)",
        "without_table": without_table,
        "with_table": with_table,
        "delta_with_table_minus_without": {
            key: with_table[key] - without_table[key]
            for key in ("win_rate", "average_gold_per_spin", "average_mineral_rarity",
                        "average_generated_minerals", "average_final_pool", "average_max_pool")
            if with_table[key] is not None and without_table[key] is not None
        },
    }


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _records_from_json(rows: list[dict[str, Any]]) -> list[GameRecord]:
    """Hydrate old sweep rows while supplying telemetry fields added later."""
    records: list[GameRecord] = []
    for row in rows:
        normalized = dict(row)
        normalized.setdefault("strategy_events", {
            "rolls": [], "deletes": [], "choices": [], "pool_curve": [],
            "final_order_curve": [],
        })
        normalized.setdefault("error", None)
        records.append(GameRecord(**normalized))
    return records


def _write(name: str, payload: dict[str, Any], markdown: str, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / f"{name}.md").write_text(markdown, encoding="utf-8")


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def _sweep_comparison(before: dict[str, Any], after: dict[str, Any]) -> tuple[dict[str, Any], str]:
    before_curve = {row["difficulty"]: row for row in before["win_rate_curve"]}
    after_curve = {row["difficulty"]: row for row in after["win_rate_curve"]}
    rows = []
    for difficulty in sorted(after_curve):
        old = before_curve[difficulty]
        new = after_curve[difficulty]
        rows.append({
            "difficulty": difficulty,
            "games": new["games"],
            "before_win_rate": old["win_rate"],
            "after_win_rate": new["win_rate"],
            "delta": new["win_rate"] - old["win_rate"],
            "before_orders": old["average_orders_completed"],
            "after_orders": new["average_orders_completed"],
            "before_final_gold": old["average_final_gold"],
            "after_final_gold": new["average_final_gold"],
            "after_order_timeout": after.get("reports", {}).get(str(difficulty), {}).get("summary", {}).get("death_reasons", {}).get("order_timeout", 0),
        })
    payload = {"seed": after["config"]["base_seed"], "rows": rows}
    lines = [
        "# Crucible Echoes 改动前后对照",
        "",
        f"固定 seed：`{payload['seed']}`；样本量沿用正式扫描。",
        "",
        "| 难度 | 局数 | 改动前通关率 | 改动后通关率 | 变化 | 前平均订单 | 后平均订单 | 前平均金币 | 后平均金币 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['difficulty']} | {row['games']} | {_pct(row['before_win_rate'])} | "
            f"{_pct(row['after_win_rate'])} | {row['delta'] * 100:+.2f}pp | "
            f"{row['before_orders']:.2f} | {row['after_orders']:.2f} | "
            f"{row['before_final_gold']:.2f}g | {row['after_final_gold']:.2f}g | timeout={row['after_order_timeout']} |"
        )
    lines += ["", "D3→D4：" + f"{(after_curve[4]['win_rate'] - after_curve[3]['win_rate']) * 100:+.2f}pp。",
              "D9→D10：" + f"{(after_curve[10]['win_rate'] - after_curve[9]['win_rate']) * 100:+.2f}pp。",
              "", "这只是同 seed 的统计对照，不把相关性直接当作因果。"]
    return payload, "\n".join(lines) + "\n"


def _pool_rows(records: list[GameRecord]) -> dict[str, Any]:
    bins = {"<=19": [], "20-25": [], "26-30": [], "31-35": [], ">35": []}
    for record in records:
        size = int(record.final_attributes.get("max_pool_size", 0))
        key = "<=19" if size <= 19 else "20-25" if size <= 25 else "26-30" if size <= 30 else "31-35" if size <= 35 else ">35"
        bins[key].append(record)
    return {
        "games": len(records),
        "average_final_pool": mean([len(r.held_ingredients) + len(r.held_equipment) for r in records]) if records else 0.0,
        "average_max_pool": mean([int(r.final_attributes.get("max_pool_size", 0)) for r in records]) if records else 0.0,
        "distribution": {key: len(value) for key, value in bins.items()},
        "win_rate_by_bin": {
            key: (sum(r.won for r in value) / len(value) if value else None)
            for key, value in bins.items()
        },
        "won_average_max_pool": mean([int(r.final_attributes.get("max_pool_size", 0)) for r in records if r.won]) if any(r.won for r in records) else None,
        "lost_average_max_pool": mean([int(r.final_attributes.get("max_pool_size", 0)) for r in records if not r.won]) if any(not r.won for r in records) else None,
    }


def _strategy_analysis(records: list[GameRecord]) -> tuple[dict[str, Any], str]:
    rolls = [event for record in records for event in record.strategy_events.get("rolls", [])]
    deletes = [event for record in records for event in record.strategy_events.get("deletes", [])]
    pool_events = [event for record in records for event in record.strategy_events.get("pool_events", [])]
    per_game = []
    for record in records:
        per_game.append({
            "seed": record.seed,
            "difficulty": record.final_attributes.get("difficulty"),
            "rolls": len(record.strategy_events.get("rolls", [])),
            "deletes": len(record.strategy_events.get("deletes", [])),
            "max_pool": record.final_attributes.get("max_pool_size"),
            "won": record.won,
        })
    payload = {
        "games": len(records),
        "average_rolls": mean([row["rolls"] for row in per_game]) if per_game else 0.0,
        "average_deletes": mean([row["deletes"] for row in per_game]) if per_game else 0.0,
        "rolls": {
            "count": len(rolls),
            "average_pool": mean([row["pool_size"] for row in rolls]) if rolls else None,
            "before_average": mean([row["before_average"] for row in rolls]) if rolls else None,
            "after_average": mean([row["after_average"] for row in rolls]) if rolls else None,
            "effective_rate": sum(bool(row["effective"]) for row in rolls) / len(rolls) if rolls else None,
            "quality_rolls": sum(bool(row["quality_roll"]) for row in rolls),
            "streak_distribution": dict(Counter(int(row["streak"]) for row in rolls)),
        },
        "deletes": {
            "count": len(deletes),
            "average_pool_before": mean([row["pool_before"] for row in deletes]) if deletes else None,
            "average_pool_after": mean([row["pool_after"] for row in deletes]) if deletes else None,
            "negative_count": sum(bool(row["negative"]) for row in deletes),
            "low_pool_deletes": sum(row["pool_before"] < 20 for row in deletes),
            "records": deletes,
        },
        "pool_origins": {
            "event_counts": dict(Counter(str(event.get("source", "unknown")) for event in pool_events)),
            "final_owned_counts": dict(Counter(
                str(origin)
                for record in records
                for origin, count in record.strategy_events.get("pool_origin_counts", {}).items()
                for _ in range(int(count))
            )),
            "average_pool_size_by_source": {
                source: mean(float(event.get("pool_size", 0)) for event in pool_events if event.get("source") == source)
                for source in sorted({str(event.get("source", "unknown")) for event in pool_events})
            },
        },
        "per_game": per_game,
        "by_difficulty": {},
    }
    for difficulty in sorted({int(row["difficulty"]) for row in per_game}):
        subset = [row for row in per_game if int(row["difficulty"]) == difficulty]
        payload["by_difficulty"][str(difficulty)] = {
            "games": len(subset),
            "average_rolls": mean(row["rolls"] for row in subset) if subset else 0.0,
            "average_deletes": mean(row["deletes"] for row in subset) if subset else 0.0,
            "win_rate": sum(bool(row["won"]) for row in subset) / len(subset) if subset else 0.0,
            "average_max_pool": mean(row["max_pool"] for row in subset) if subset else 0.0,
        }
    lines = [
        "# heuristic-v1 Roll / 删除诊断", "",
        f"样本：{len(records)} 局。Roll 总次数 {len(rolls)}，删除总次数 {len(deletes)}。", "",
        f"- 平均 Roll：{payload['average_rolls']:.2f}；有效 Roll：{_pct(payload['rolls']['effective_rate'])}",
        f"- Roll 前平均估值：{payload['rolls']['before_average']}; Roll 后：{payload['rolls']['after_average']}",
        f"- 平均删除：{payload['average_deletes']:.2f}；删除前平均池：{payload['deletes']['average_pool_before']}",
        f"- 有优质候选仍 Roll：{payload['rolls']['quality_rolls']} 次；池小于20时删除：{payload['deletes']['low_pool_deletes']} 次。", "",
        "策略没有针对具体卡牌 ID 加隐藏分；这些统计来自统一候选估值和当前状态。",
    ]
    return payload, "\n".join(lines) + "\n"


def _value_analysis(records: list[GameRecord]) -> tuple[dict[str, Any], str]:
    target_ids = set(FOCUS_INGREDIENTS) | set(FOCUS_ITEMS)
    data: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"selected": [], "abandoned": [], "components": []})
    for record in records:
        for choice in record.strategy_events.get("choices", []):
            for def_id, details in choice.get("offers", {}).items():
                if def_id not in target_ids:
                    continue
                score = float(details.get("score", 0.0))
                if choice.get("selected") == def_id:
                    data[def_id]["selected"].append(score)
                    data[def_id]["components"].append(details.get("components", {}))
                else:
                    data[def_id]["abandoned"].append(score)
    result = {}
    for def_id in sorted(target_ids):
        row = data[def_id]
        component_totals = Counter()
        for components in row["components"]:
            component_totals.update({key: float(value) for key, value in components.items()})
        result[def_id] = {
            "selected_count": len(row["selected"]),
            "abandoned_count": len(row["abandoned"]),
            "selected_average": mean(row["selected"]) if row["selected"] else None,
            "abandoned_average": mean(row["abandoned"]) if row["abandoned"] else None,
            "selected_components": {key: value / len(row["components"]) for key, value in component_totals.items()} if row["components"] else {},
        }
    lines = ["# 长期收益估值构成", "", "数值是 heuristic 的可解释评分组成，不是游戏金币，也不直接证明卡牌强弱。", "",
             "| ID | 选择样本 | 放弃样本 | 选择时估值 | 放弃时估值 | 即时 | 长期 | 联动 | 风险 | 池压力 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for def_id, row in result.items():
        components = row["selected_components"]
        lines.append(
            f"| `{def_id}` | {row['selected_count']} | {row['abandoned_count']} | "
            f"{row['selected_average']} | {row['abandoned_average']} | "
            f"{components.get('immediate', 0):.2f} | {components.get('long_term', 0):.2f} | "
            f"{components.get('synergy', 0):.2f} | {components.get('risk', 0):.2f} | {components.get('pool_pressure', 0):.2f} |"
        )
    return result, "\n".join(lines) + "\n"


def _archetypes(records: list[GameRecord], catalog: Catalog) -> tuple[dict[str, Any], str]:
    # Use the strategy's observed tag/mechanism state. This intentionally
    # precedes the legacy named-category code below, which remains unreachable
    # for compatibility but no longer drives the report.
    rows = []
    for record in records:
        state = record.strategy_events.get("final_build_state", {})
        primary_tags = list(state.get("primary_tags", []))
        mechanism_tags = list(state.get("mechanism_tags", []))
        primary = f"tag:{primary_tags[0]}" if primary_tags else "mixed"
        secondary = [f"tag:{tag}" for tag in primary_tags[1:3]]
        if mechanism_tags:
            secondary.append(f"mechanism:{mechanism_tags[0]}")
        rows.append({
            "seed": record.seed, "primary": primary, "secondary": secondary[:2],
            "primary_tags": primary_tags, "mechanism_tags": mechanism_tags,
            "origin_counts": state.get("origin_counts", record.strategy_events.get("pool_origin_counts", {})),
            "won": record.won, "max_pool": record.final_attributes.get("max_pool_size"),
            "rolls": len(record.strategy_events.get("rolls", [])),
            "deletes": len(record.strategy_events.get("deletes", [])),
            "gold": record.gold, "orders": record.orders_completed,
        })
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["primary"]].append(row)
    summary = {
        name: {
            "games": len(value),
            "appearance_rate": len(value) / len(rows) if rows else 0,
            "win_rate": sum(row["won"] for row in value) / len(value) if value else 0,
            "average_max_pool": mean(row["max_pool"] for row in value) if value else 0,
            "average_rolls": mean(row["rolls"] for row in value) if value else 0,
            "average_deletes": mean(row["deletes"] for row in value) if value else 0,
            "average_gold": mean(row["gold"] for row in value) if value else 0,
            "average_orders": mean(row["orders"] for row in value) if value else 0,
            "mechanism_rate": sum(bool(row["mechanism_tags"]) for row in value) / len(value) if value else 0,
        } for name, value in grouped.items()
    }
    return {"summary": summary, "games": rows}, json.dumps(summary, ensure_ascii=False, indent=2) + "\n"

    rules = {
        "猫": lambda tags, items: "cat" in tags,
        "矿物/金属": lambda tags, items: bool(tags & {"stone", "ore", "metal"}),
        "火焰/燃烧": lambda tags, items: "fire" in tags or "burned" in tags,
        "垃圾/删除": lambda tags, items: "waste" in tags or "warehouse_manager" in items,
        "魔法": lambda tags, items: "magic" in tags or "magic_filter" in items,
        "人类": lambda tags, items: "human" in tags,
        "动物": lambda tags, items: "animal" in tags,
        "箱子/钥匙": lambda tags, items: bool(tags & {"chest", "key"}),
        "试剂": lambda tags, items: any(item.endswith("_reagent") for item in items),
        "稀有度/幸运": lambda tags, items: any(catalog.items.get(item, {}).get("rarity_multiplier", 1) > 1 for item in items),
        "大池": lambda tags, items: False,
        "小池/精简": lambda tags, items: False,
        "泛经济": lambda tags, items: bool(items),
    }
    rows = []
    for record in records:
        ids = record.held_ingredients + record.held_equipment
        tags = {tag for def_id in ids for tag in catalog.ingredients.get(def_id, {}).get("tags", [])}
        items = set(record.held_items)
        scores = Counter()
        for name, predicate in rules.items():
            if predicate(tags, items):
                scores[name] += 1
        if int(record.final_attributes.get("max_pool_size", 0)) > 30:
            scores["大池"] += 1
        else:
            scores["小池/精简"] += 1
        primary = scores.most_common(1)[0][0] if scores else "泛经济"
        secondary = [name for name, _ in scores.most_common(3) if name != primary][:2]
        rows.append({"seed": record.seed, "primary": primary, "secondary": secondary, "won": record.won,
                     "max_pool": record.final_attributes.get("max_pool_size"),
                     "rolls": len(record.strategy_events.get("rolls", [])),
                     "deletes": len(record.strategy_events.get("deletes", [])),
                     "gold": record.gold, "orders": record.orders_completed})
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["primary"]].append(row)
    summary = {
        name: {
            "games": len(value),
            "appearance_rate": len(value) / len(rows) if rows else 0,
            "win_rate": sum(row["won"] for row in value) / len(value) if value else 0,
            "average_max_pool": mean(row["max_pool"] for row in value) if value else 0,
            "average_rolls": mean(row["rolls"] for row in value) if value else 0,
            "average_deletes": mean(row["deletes"] for row in value) if value else 0,
            "average_gold": mean(row["gold"] for row in value) if value else 0,
            "average_orders": mean(row["orders"] for row in value) if value else 0,
        } for name, value in grouped.items()
    }
    return {"summary": summary, "games": rows}, json.dumps(summary, ensure_ascii=False, indent=2) + "\n"


def _failure_success(records: list[GameRecord]) -> tuple[dict[str, Any], dict[str, Any]]:
    failures = [record for record in records if not record.won]
    def cluster(record: GameRecord) -> str:
        max_pool = int(record.final_attributes.get("max_pool_size", 0))
        if record.final_attributes.get("difficulty") == 10 and record.end_layer >= 13:
            return "D10最终订单失败"
        if max_pool > 30:
            return "池过大导致稀释"
        if max_pool < 20:
            return "池过小导致产出不足"
        if record.end_layer <= 3:
            return "前期经济不足"
        if record.end_layer >= 10:
            return "后期订单曲线压死"
        if len(record.strategy_events.get("rolls", [])) == 0 and len(record.strategy_events.get("deletes", [])) == 0:
            return "Roll / 删除资源不足"
        return "中期成长不足"
    grouped = defaultdict(list)
    for record in failures:
        grouped[cluster(record)].append(record)
    failure_payload = {
        name: {
            "count": len(value),
            "rate_of_failures": len(value) / len(failures) if failures else 0,
            "average_layer": mean(record.end_layer for record in value) if value else 0,
            "average_pool": mean(record.final_attributes.get("max_pool_size", 0) for record in value) if value else 0,
            "average_gold": mean(record.gold for record in value) if value else 0,
            "example_seeds": [record.seed for record in value[:5]],
        } for name, value in grouped.items()
    }
    successes = sorted((record for record in records if record.won), key=lambda record: (record.spins, -record.gold))
    success_payload = {
        "fastest": [{"seed": record.seed, "spins": record.spins, "gold": record.gold, "orders": record.orders_completed,
                      "max_pool": record.final_attributes.get("max_pool_size"), "items": record.held_items[:10],
                      "ingredients": record.held_ingredients[:15]} for record in successes[:30]],
        "most_difficult_wins": [{"seed": record.seed, "spins": record.spins, "gold": record.gold,
                                  "max_pool": record.final_attributes.get("max_pool_size")} for record in successes[-30:]],
        "by_difficulty": {},
    }
    for difficulty in range(1, 11):
        wins = [record for record in records if record.won and int(record.final_attributes.get("difficulty", 0)) == difficulty]
        wins.sort(key=lambda record: (record.spins, -record.gold))
        success_payload["by_difficulty"][str(difficulty)] = {
            "games": sum(int(record.final_attributes.get("difficulty", 0)) == difficulty for record in records),
            "wins": len(wins),
            "fastest_three": [{"seed": record.seed, "spins": record.spins, "gold": record.gold,
                               "max_pool": record.final_attributes.get("max_pool_size")} for record in wins[:3]],
            "most_difficult_three": [{"seed": record.seed, "spins": record.spins, "gold": record.gold,
                                      "max_pool": record.final_attributes.get("max_pool_size")} for record in wins[-3:]],
        }
    return failure_payload, success_payload


def _final_order_summary(records: list[GameRecord], telemetry_records: list[GameRecord] | None = None) -> dict[str, Any]:
    """Summarize the D10 1350g/15-spin final order from telemetry samples."""
    d10 = [record for record in records if int(record.final_attributes.get("difficulty", 0)) == 10]
    started = [record for record in d10 if record.orders_completed >= 12]
    completed = [record for record in started if record.won and record.orders_completed >= 13]
    progress: list[float] = []
    for record in started:
        # The persisted full-sweep rows contain final gold but not a turn-by-
        # turn order trace.  For losses this is the exact timeout progress;
        # wins are counted as complete.  Detailed telemetry, when available,
        # supplies the finer pre-settlement curve below.
        progress.append(1.0 if record.won else min(1.0, max(0.0, record.gold / 1350.0)))
    telemetry_records = telemetry_records or []
    telemetry_progress: list[float] = []
    for record in telemetry_records:
        for event in record.strategy_events.get("final_order_curve", []):
            target = max(1, int(event.get("target", 1350)))
            telemetry_progress.append(min(1.0, float(event.get("gold_before_spin", 0)) / target))
    return {
        "games": len(d10),
        "final_order_started": len(started),
        "final_order_start_rate": len(started) / len(d10) if d10 else 0.0,
        "final_order_completed": len(completed),
        "completion_rate_after_start": len(completed) / len(started) if started else 0.0,
        "average_progress_at_end": mean(progress) if progress else None,
        "average_progress_before_final_order_spin": mean(telemetry_progress) if telemetry_progress else None,
        "progress_samples": len(progress),
        "telemetry_progress_samples": len(telemetry_progress),
        "progress_definition": "full sweep: timeout final gold / 1350 and wins=1; optional telemetry curve is gold_before_spin / target",
    }


def _focused_ab(seed: int, games: int, catalog: Catalog, difficulty: int, rejected_id: str) -> dict[str, Any]:
    normal = run_batch(games=games, seed=seed, difficulty=difficulty, catalog=catalog)
    rejected = run_batch(games=games, seed=seed, difficulty=difficulty, catalog=catalog,
                         strategy=RejectItemStrategy(rejected_id))
    normal_records = _records_from_json(normal.games_detail)
    rejected_records = _records_from_json(rejected.games_detail)

    def extras(records: list[GameRecord]) -> dict[str, Any]:
        over30_turns = []
        extra_income = []
        acquired = 0
        for record in records:
            curve = record.strategy_events.get("pool_curve", [])
            over30_turns.append(sum(int(row.get("pool_size", 0)) > 30 for row in curve))
            extra_income.append(sum(max(0, int(row.get("income", 0))) for row in curve))
            acquired += int(record.content_stats.get("items", {}).get(rejected_id, {}).get("acquisition_count", 0))
        return {
            "average_over30_spins": mean(over30_turns) if over30_turns else 0.0,
            "average_total_income": mean(extra_income) if extra_income else 0.0,
            "acquisitions": acquired,
        }
    return {
        "difficulty": difficulty, "games": games, "item": rejected_id,
        "normal_win_rate": normal.summary["win_rate"], "reject_win_rate": rejected.summary["win_rate"],
        "delta": rejected.summary["win_rate"] - normal.summary["win_rate"],
        "normal_average_gold": normal.summary["average_final_gold"],
        "reject_average_gold": rejected.summary["average_final_gold"],
        "normal_average_max_pool": normal.summary["average_max_pool_size"],
        "reject_average_max_pool": rejected.summary["average_max_pool_size"],
        "normal": extras(normal_records),
        "reject": extras(rejected_records),
    }


def _buff_ab(seed: int, games: int, catalog: Catalog) -> list[dict[str, Any]]:
    rows = []

    def content_metrics(report: Any, def_id: str) -> dict[str, Any]:
        all_rows = [row for category in report.content.values() for row in category if row.get("id") == def_id]
        row = all_rows[0] if all_rows else {}
        return {
            "offers": int(row.get("offer_count", 0)),
            "choices": int(row.get("choice_count", 0)),
            "acquisitions": int(row.get("acquisition_count", 0)),
            "selection_rate": int(row.get("choice_count", 0)) / int(row.get("offer_count", 0)) if row.get("offer_count") else None,
            "possession_rate": int(row.get("final_owned_games", 0)) / int(report.games) if report.games else None,
            "owned_win_rate": row.get("win_rate_when_owned"),
        }

    for item_id, (kind, def_id, old_fields) in BUFFS.items():
        old = copy.deepcopy(catalog)
        active = copy.deepcopy(catalog)
        target = old.ingredients[def_id] if kind == "ingredients" else old.items[def_id]
        target.update(copy.deepcopy(old_fields))
        active_target = active.ingredients[def_id] if kind == "ingredients" else active.items[def_id]
        # The active catalog is the current value; the old catalog is the
        # counterfactual.  Keep the same seed and policy for both sides.
        normal = run_batch(games=games, seed=seed, difficulty=1, catalog=active)
        previous = run_batch(games=games, seed=seed, difficulty=1, catalog=old)
        rows.append({"id": item_id, "games": games, "old_win_rate": previous.summary["win_rate"],
                     "new_win_rate": normal.summary["win_rate"], "delta": normal.summary["win_rate"] - previous.summary["win_rate"],
                     "old_gold": previous.summary["average_final_gold"], "new_gold": normal.summary["average_final_gold"],
                     "old_content": content_metrics(previous, def_id),
                     "new_content": content_metrics(normal, def_id)})
    return rows


def run_diagnostics(*, before_json: Path, after_json: Path, output_dir: Path, seed: int = 424242,
                    detail_games: int = 100, ab_games: int = 100, buff_games: int = 100) -> dict[str, Any]:
    before = _load_json(before_json)
    after = _load_json(after_json)
    after_curve = {row["difficulty"]: row for row in after["win_rate_curve"]}
    comparison, comparison_md = _sweep_comparison(before, after)
    _write("balance_before_after_comparison", comparison, comparison_md, output_dir)
    (output_dir / "balance_after_full.md").write_text((output_dir.parent / "balance_sweep_after.md").read_text(encoding="utf-8"), encoding="utf-8")
    (output_dir / "balance_after_full.json").write_text(after_json.read_text(encoding="utf-8"), encoding="utf-8")

    catalog = Catalog.load()
    full_after_records = {
        difficulty: _records_from_json(after["reports"][str(difficulty)]["games"])
        for difficulty in range(1, 11)
    }
    official_pool = {
        str(difficulty): _pool_rows(records)
        for difficulty, records in full_after_records.items()
    }
    detailed_records: list[GameRecord] = []
    for difficulty in range(1, 11):
        report = run_batch(games=detail_games, seed=seed, difficulty=difficulty, catalog=catalog)
        detailed_records.extend(GameRecord(**row) for row in report.games_detail)
    strategy_payload, strategy_md = _strategy_analysis(detailed_records)
    _write("reroll_delete_analysis", strategy_payload, strategy_md, output_dir)
    pool_payload = {"by_difficulty": {}, "official_sweep_by_difficulty": official_pool}
    for difficulty in range(1, 11):
        subset = [record for record in detailed_records if record.final_attributes.get("difficulty") == difficulty]
        pool_payload["by_difficulty"][str(difficulty)] = _pool_rows(subset)
    _write("heuristic_strategy_analysis", pool_payload, "# heuristic-v1 池控诊断\n\n" + json.dumps(pool_payload, ensure_ascii=False, indent=2) + "\n", output_dir)
    value_payload, value_md = _value_analysis(detailed_records)
    _write("long_term_value_analysis", value_payload, value_md, output_dir)
    archetype_payload, archetype_md = _archetypes(detailed_records, catalog)
    _write("archetype_analysis", archetype_payload, "# 构筑 archetype 诊断\n\n" + archetype_md, output_dir)
    failure_payload, success_payload = _failure_success(detailed_records)
    _write("failure_cluster_analysis", failure_payload, "# 失败局聚类\n\n" + json.dumps(failure_payload, ensure_ascii=False, indent=2) + "\n", output_dir)
    _write("successful_run_analysis", success_payload, "# 成功局诊断\n\n" + json.dumps(success_payload, ensure_ascii=False, indent=2) + "\n", output_dir)

    ab_rows = [_focused_ab(seed, ab_games, catalog, difficulty, "impossible_container") for difficulty in (1, 5, 10)]
    _write("impossible_container_ab", {"rows": ab_rows}, "# 不可能容器 A/B\n\n" + json.dumps(ab_rows, ensure_ascii=False, indent=2) + "\n", output_dir)
    buff_rows = _buff_ab(seed, buff_games, catalog)
    _write("buff_ab_comparison", {"rows": buff_rows}, "# 关键 Buff A/B\n\n" + json.dumps(buff_rows, ensure_ascii=False, indent=2) + "\n", output_dir)

    # Direct interaction summary for the two newly sensitive mechanisms.
    lucky = GameEngine(catalog); lucky.new_game(seed); lucky._apply_potion_payload(catalog.ingredients["lucky_potion"]["potion"], "diagnostic")
    lucky.s.pending.append(lucky.make_choice("ingredient")); lucky.s.tokens["roll"] = 1; lucky.reroll()
    before_roll = lucky.s.flags.get("choice_minimum_count")
    lucky.choose(1); after_choice = lucky.s.flags.get("choice_minimum_count", 0)
    plain_ore = GameEngine(catalog); plain_ore.new_game(seed); plain_ore.s.ingredients.clear()
    plain_levels = []
    for _ in range(1000):
        instance = plain_ore._spawn_random(tag="stone", rarity=1)
        plain_levels.append(int(catalog.ingredients[instance.def_id]["rarity"]))
    ore = GameEngine(catalog); ore.new_game(seed); ore.s.items.append("ore_sorting_table"); ore.s.ingredients.clear()
    ore_levels = []
    for _ in range(1000):
        instance = ore._spawn_random(tag="stone", rarity=1)
        ore_levels.append(int(catalog.ingredients[instance.def_id]["rarity"]))
    interaction_payload = {"lucky_potion": {"remaining_after_roll": before_roll, "remaining_after_formal_choice": after_choice},
                           "ore_sorting_table": {"samples": len(ore_levels), "minimum": min(ore_levels), "average_rarity": mean(ore_levels),
                                                  "without_table_minimum": min(plain_levels), "without_table_average_rarity": mean(plain_levels)}}
    _write("lucky_potion_interaction_tests", interaction_payload, "# lucky_potion interaction tests\n\n" + json.dumps(interaction_payload, ensure_ascii=False, indent=2) + "\n", output_dir)
    ore_flow = _ore_flow_ab(seed, 1000, catalog)
    ore_analysis = {**interaction_payload["ore_sorting_table"], "mineral_flow_ab": ore_flow}
    _write("ore_sorting_table_analysis", ore_analysis, "# ore_sorting_table analysis\n\n" + json.dumps(ore_analysis, ensure_ascii=False, indent=2) + "\n", output_dir)

    stability = {"long_games": len(run_batch(games=100, seed=seed + 77, difficulty=10).games_detail), "seed_reproducible": True,
                 "save_restore_checks": 0, "save_restore_failures": 0}
    for index in range(200):
        engine = GameEngine(catalog); engine.new_game(seed + index)
        if engine.s.pending: engine.choose(1)
        snapshot = GameState.from_dict(engine.s.to_dict())
        # Continue the live branch and compare it with a branch restored from
        # the exact serialized checkpoint.  Comparing two restored copies
        # would only prove that deserialization is deterministic, not that a
        # save can resume the same game trajectory.
        live = engine
        restored = GameEngine(catalog).bind(GameState.from_dict(snapshot.to_dict()))
        live.spin(); restored.spin(); stability["save_restore_checks"] += 1
        # bind() adds safe defaults for fields absent from legacy saves; apply
        # the same normalization to the uninterrupted branch before comparing.
        live_normalized = GameEngine(catalog).bind(GameState.from_dict(live.s.to_dict())).s.to_dict()
        if live_normalized != restored.s.to_dict(): stability["save_restore_failures"] += 1
    for index in range(100):
        first = simulate_game(seed + index, difficulty=(index % 10) + 1, catalog=catalog)
        second = simulate_game(seed + index, difficulty=(index % 10) + 1, catalog=catalog)
        if first.to_dict() != second.to_dict(): stability["seed_reproducible"] = False
    _write("simulation_stability_report", stability, "# 模拟稳定性报告\n\n" + json.dumps(stability, ensure_ascii=False, indent=2) + "\n", output_dir)

    d10_full_records = full_after_records[10]
    final_order = _final_order_summary(d10_full_records, detailed_records)
    timeout_by_difficulty = {
        str(difficulty): after["reports"][str(difficulty)]["summary"].get("death_reasons", {}).get("order_timeout", 0)
        for difficulty in range(1, 11)
    }
    executive = {
        "systemic_issues": ["D10 通关率显著低于D9，最终订单形成明确瓶颈。", "大池分布仍占主导，策略软上限尚未实现为硬约束。", "生成/移除链可能制造极端大池，需区分策略问题与内容生成问题。", "Roll/删除资源在早期不足，低层失败无法依靠清池修复。", "相关性报告仍受幸存者偏差影响。"],
        "strong_watch": ["魔法滤网100%抵抗", "幸运护符/罗盘的稀有度提升", "选矿台全生成保底", "D10最终订单", "大池生成链"],
        "weak_watch": ["纸张", "动物登记册", "大型反应炉", "小保险箱", "低样本稀有内容"],
        "heuristic_bias": ["选择率受候选稀有度表影响", "删除只受Token供给限制", "生成型成分的池成本估计仍粗粒度", "大池样本可能是生成链而非选择失误", "胜率差不能直接归因于单卡"],
        # Adjacent difficulty changes are measured on the current (after)
        # curve.  The before/after delta column answers a different question
        # and must not be used as a D3->D4 or D9->D10 jump.
        "d3_to_d4": (after_curve[4]["win_rate"] - after_curve[3]["win_rate"]),
        "d9_to_d10": (after_curve[10]["win_rate"] - after_curve[9]["win_rate"]),
        "pool_control": pool_payload,
        "order_timeout_by_difficulty": timeout_by_difficulty,
        "d10_final_order": final_order,
        "focused_ab_games_per_side": ab_games,
        "buff_ab_games_per_side": buff_games,
        "sample_note": "专项 A/B 与策略诊断为定向样本；正式难度曲线使用附件要求的 1000/500 局扫描。",
        "simulation_stability": stability,
        "simulation_bug_found": True,
        "simulation_bug_fixes": [{
            "issue": "paper script could raise NameError when its growth check executed",
            "cause": "the script branch referenced an undefined ingredient definition",
            "fix": "resolve the current ingredient definition once at _run_script entry",
            "regression_test": "test_paper_uses_forty_two_percent_growth_chance",
        }],
    }
    _write("balance_executive_summary", executive, "# 平衡诊断执行摘要\n\n" + json.dumps(executive, ensure_ascii=False, indent=2) + "\n", output_dir)
    return executive


def main() -> int:
    parser = argparse.ArgumentParser(description="Run report-only deep balance diagnostics")
    parser.add_argument("--before-json", default="reports/balance_sweep_latest.json")
    parser.add_argument("--after-json", default="reports/balance_sweep_after.json")
    parser.add_argument("--output-dir", default="reports/deep_diagnostics")
    parser.add_argument("--seed", type=int, default=424242)
    parser.add_argument("--detail-games", type=int, default=100)
    parser.add_argument("--ab-games", type=int, default=100)
    parser.add_argument("--buff-games", type=int, default=100)
    args = parser.parse_args()
    run_diagnostics(before_json=Path(args.before_json), after_json=Path(args.after_json), output_dir=Path(args.output_dir),
                    seed=args.seed, detail_games=args.detail_games, ab_games=args.ab_games, buff_games=args.buff_games)
    print(f"diagnostics written to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
