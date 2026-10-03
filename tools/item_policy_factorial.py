"""Analysis-only 2x2 item pick/reroll ablation with independent policy instances.

python -m tools.item_policy_factorial --games 100 --seed 20261033
--difficulties 7 10 15 --output reports/item_factorial.json
Completed cells are resumable; game rules and registered strategies never change.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from crucible_echoes.action_strategy import ContentActionStrategy
from crucible_echoes.balance_experiment import catalog_hash, compare
from crucible_echoes.catalog import Catalog
from crucible_echoes.item_strategy import ItemEconomyStrategy
from crucible_echoes.provenance import capture_snapshot
from crucible_echoes.simulation import SimulationStrategy, derive_seed, run_batch
from crucible_echoes.strategy_benchmark import atomic_json, benchmark_lock, compact_report, merge_reports, source_fingerprint
from tools.paired_item_trace import publish_report


CELLS = {"pick3_roll3": (3, 3), "pick4_roll3": (4, 3),
         "pick3_roll4": (3, 4), "pick4_roll4": (4, 4)}
CONTRASTS = {"pick_at_roll3": ("pick3_roll3", "pick4_roll3"),
             "pick_at_roll4": ("pick3_roll4", "pick4_roll4"),
             "roll_at_pick3": ("pick3_roll3", "pick3_roll4"),
             "roll_at_pick4": ("pick4_roll3", "pick4_roll4"),
             "combined": ("pick3_roll3", "pick4_roll4")}


class ItemPolicyFactorial(SimulationStrategy):
    """Delegate whole bound methods, not a v3 method bound to a v4 receiver.

    That tempting shortcut would still dynamically dispatch self.score to v4
    and fail to isolate the reroll factor. All non-item decisions use v3.
    """

    _GENERATOR_FIELDS = ContentActionStrategy._GENERATOR_FIELDS

    def __init__(self, pick_version=3, roll_version=3):
        if pick_version not in (3, 4) or roll_version not in (3, 4):
            raise ValueError("Item policy versions must be 3 or 4")
        factory = {3: ContentActionStrategy, 4: ItemEconomyStrategy}
        self.baseline = ContentActionStrategy()
        self.pick_policy = factory[pick_version]()
        self.roll_policy = factory[roll_version]()
        self.name = f"analysis-item-pick{pick_version}-roll{roll_version}"

    def choose(self, engine, choice):
        policy = self.pick_policy if choice.kind == "item" else self.baseline
        return policy.choose(engine, choice)

    def should_reroll(self, engine, choice):
        policy = self.roll_policy if choice.kind == "item" else self.baseline
        return policy.should_reroll(engine, choice)

    def score(self, engine, kind, def_id):
        policy = self.pick_policy if kind == "item" else self.baseline
        return policy.score(engine, kind, def_id)

    def score_components(self, engine, kind, def_id):
        policy = self.pick_policy if kind == "item" else self.baseline
        return policy.score_components(engine, kind, def_id)

    def removal_index(self, engine):
        return self.baseline.removal_index(engine)

    def bundle_index(self, engine, choice):
        return self.baseline.bundle_index(engine, choice)

    def pre_spin_action(self, engine):
        return self.baseline.pre_spin_action(engine)

    def build_state(self, engine):
        return self.baseline.build_state(engine)

    def instance_retention_components(self, engine, instance):
        return self.baseline.instance_retention_components(engine, instance)


def validate_cell(part, *, seed, difficulty, start, count, cell):
    expected = {"base_seed": seed, "difficulty": difficulty, "games": count,
                "strategy": ItemPolicyFactorial(*CELLS[cell]).name, "fun_mode": "none"}
    if part["config"] != expected or len(part["games"]) != count:
        raise ValueError("Cell configuration/count mismatch")
    summary = part["summary"]
    if summary["games_recorded"] != count or summary["aborted"]:
        raise ValueError("Incomplete/aborted cells are not valid outcomes")
    if summary["wins"] + summary["losses"] != count:
        raise ValueError("Cell outcome totals do not reconcile")
    if summary.get("pool_flow_unreconciled_games", 0):
        raise ValueError("Unreconciled pool flow")
    for index, game in enumerate(part["games"], start):
        if game["index"] != index or game["seed"] != derive_seed(seed, index):
            raise ValueError("Cell index/seed mismatch")
        if game["status"] not in ("won", "lost") or game["won"] != (game["status"] == "won"):
            raise ValueError("Invalid terminal outcome")
    if sum(bool(game["won"]) for game in part["games"]) != summary["wins"]:
        raise ValueError("Cell wins do not match records")


def summarize_cells(parts):
    merged = {cell: merge_reports(rows) for cell, rows in parts.items()}
    result = {"games": len(merged["pick3_roll3"].games_detail),
              "cells": {cell: {"summary": report.summary, "content": report.content}
                        for cell, report in merged.items()}, "contrasts": {}}
    for name, (left, right) in CONTRASTS.items():
        contrast = compare(merged[left], merged[right], allow_strategy_difference=True)
        # Aggregates/content are already retained once per cell.
        for key in ("baseline", "candidate", "content_before", "content_after"):
            contrast.pop(key)
        result["contrasts"][name] = contrast
    rates = {cell: report.summary["win_rate"] for cell, report in merged.items()}
    result["descriptive_interaction_pp"] = 100 * (
        rates["pick4_roll4"] - rates["pick3_roll4"] - rates["pick4_roll3"] + rates["pick3_roll3"])
    return result


def markdown(report):
    config = report["config"]
    lines = ["# 物品选择 × 重调策略四组合消融", "",
             "状态：" + ("全部完成" if report["complete"] else "阶段结果，计划未完成"),
             f"Seed `{config['seed']}`；none模式；每难度/组合计划{config['games']}局。",
             "3=content-v3，4=content-v4；其余操作全部采用v3。卡表、核心与默认策略未修改。",
             "相同初始seed不意味着分叉后抽取相同。区间为配对近似区间，p未做多重比较校正。",
             "少于25个不一致胜负配对：样本不足，不宣布策略优势或单卡因果。交互项仅描述性。", ""]
    for difficulty, result in report["difficulties"].items():
        lines += [f"## D{difficulty}（{result['games']}个共同seed）", "",
                  "| 选择/重调 | 通关 | 订单均值 | 最大池均值 | >30池 | 平均重调 | 主动抓取 | 自动生成 |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for cell, entry in result["cells"].items():
            s = entry["summary"]
            lines.append(f"| {cell} | {s['wins']}/{s['games_recorded']} ({s['win_rate']:.1%}) | "
                         f"{s['average_orders_completed']:.2f} | {s['average_max_pool_size']:.2f} | "
                         f"{s['pool_over_30_rate']:.1%} | {s['average_rolls']:.2f} | "
                         f"{s['active_choice_total']} | {s['automatic_generation_total']} |")
        lines += ["", "| 因子对照 | 差值pp | 新增/丢失通关 | 精确p | 证据 |",
                  "|---|---:|---:|---:|---|"]
        for name, c in result["contrasts"].items():
            lines.append(f"| {name} | {c['win_delta']*100:+.2f} | {c['gained_wins']}/{c['lost_wins']} | "
                         f"{c['paired_exact_p']:.4f} | {c['evidence']} |")
        lines += ["", f"描述性交互项：{result['descriptive_interaction_pp']:+.2f}pp（无独立显著性声明）。", "",
                  "### 订单条件死亡率", "", "| 订单 | pick3_roll3 | pick4_roll3 | pick3_roll4 | pick4_roll4 |",
                  "|---|---|---|---|---|"]
        rows = {cell: {r['order']: r for r in entry['summary']['order_progression']}
                for cell, entry in result['cells'].items()}
        for order in sorted({order for cell in rows.values() for order in cell}):
            values = []
            for cell in CELLS:
                r = rows[cell].get(order)
                values.append("—" if not r or not r['reached'] else
                              f"{r['died']}/{r['reached']} ({r['conditional_death_rate']:.1%})")
            lines.append(f"| {order} | " + " | ".join(values) + " |")
    lines += ["", "JSON保留各组合完整汇总、候选曝光/选择/持有统计和配对区间；分块保留实际seed/胜负。",
              "自动生成统计不是完整UID级因果谱系；净池仅动作边界观测。未到达订单为—，不是0%死亡。"]
    return "\n".join(lines) + "\n"


def run_experiment(*, games, seed, difficulties, output, chunk_size=25, resume=False, max_cells=None):
    if games < 1 or chunk_size < 1 or not difficulties or len(set(difficulties)) != len(difficulties):
        raise ValueError("Invalid game/chunk/difficulty count")
    if any(not 1 <= d <= 15 for d in difficulties) or (max_cells is not None and max_cells < 0):
        raise ValueError("Invalid difficulty/max_cells")
    output = Path(output)
    catalog = Catalog.load()
    config = {"games": games, "seed": seed, "difficulties": sorted(difficulties), "chunk_size": chunk_size,
              "fun_mode": "none", "cells": {k: list(v) for k, v in CELLS.items()}}
    source = Path(__file__).read_text(encoding="utf-8")

    def fingerprint():
        return {"source": source_fingerprint(), "catalog": catalog_hash(Catalog.load()), "python": sys.version,
                "analysis": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "publisher": hashlib.sha256(Path(__file__).with_name("paired_item_trace.py").read_bytes()).hexdigest()}

    frozen = fingerprint()
    directory = output.with_suffix(".cells")
    manifest_path = directory / "manifest.json"
    with benchmark_lock(output.with_suffix(".lock")):
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not resume or manifest['config'] != config or manifest['fingerprint'] != frozen:
                raise ValueError("Resume refused: configuration/source/catalog changed or --resume missing")
        else:
            if resume or output.exists() or output.with_suffix(".snapshot.zip").exists():
                raise ValueError("Choose a new output or a matching resumable manifest")
            manifest = {"config": config, "fingerprint": frozen, "analysis_source": source,
                        "snapshot": capture_snapshot(output.with_suffix(".snapshot.zip"), catalog)}
            publish_report(manifest_path, manifest, atomic_json)
        report = {"protocol": "crucible-echoes-item-policy-factorial/v1", **manifest,
                  "complete": False, "difficulties": {}}
        executed = 0
        for difficulty in sorted(difficulties):
            parts = {cell: [] for cell in CELLS}
            for start in range(0, games, chunk_size):
                count = min(chunk_size, games - start)
                chunk = {}
                for cell, versions in CELLS.items():
                    path = directory / f"d{difficulty}_{start:06d}_{cell}.json"
                    if fingerprint() != frozen:
                        raise ValueError("Frozen source/catalog changed; do not combine versions")
                    if path.exists():
                        part = json.loads(path.read_text(encoding="utf-8"))
                    else:
                        if max_cells is not None and executed >= max_cells:
                            break
                        print(f"D{difficulty} {cell}: {start+1}..{start+count}", flush=True)
                        part = compact_report(run_batch(count, seed, difficulty, catalog=catalog,
                            strategy=ItemPolicyFactorial(*versions), start_index=start))
                        validate_cell(part, seed=seed, difficulty=difficulty, start=start, count=count, cell=cell)
                        if fingerprint() != frozen:
                            raise ValueError("Frozen source/catalog changed; cell not published")
                        publish_report(path, part, atomic_json)
                        executed += 1
                    validate_cell(part, seed=seed, difficulty=difficulty, start=start, count=count, cell=cell)
                    chunk[cell] = part
                if len(chunk) != len(CELLS):
                    break  # Never compare unequal subsets after a partial chunk.
                for cell in CELLS:
                    parts[cell].append(chunk[cell])
                report['difficulties'][str(difficulty)] = summarize_cells(parts)
                publish_report(output, report, atomic_json)
                output.with_suffix('.md').write_text(markdown(report), encoding='utf-8')
        report['complete'] = len(report['difficulties']) == len(difficulties) and all(
            r['games'] == games for r in report['difficulties'].values())
        publish_report(output, report, atomic_json)
        output.with_suffix('.md').write_text(markdown(report), encoding='utf-8')
        return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--games', type=int, default=100)
    p.add_argument('--seed', type=int, default=20261033)
    p.add_argument('--difficulties', nargs='+', type=int, default=[7, 10, 15])
    p.add_argument('--chunk-size', type=int, default=25)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--resume', action='store_true')
    run_experiment(**vars(p.parse_args()))


if __name__ == '__main__':
    main()
