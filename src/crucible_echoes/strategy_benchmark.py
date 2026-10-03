"""Resumable, frozen-catalog paired policy comparisons through the real engine.

Each completed chunk is atomically saved. Same seed/index protocol as run_batch;
changing source, catalog, strategies or run arguments requires a new output path.
No game data or default policy is changed by this tool.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from .balance_experiment import catalog_hash, compare
from .catalog import Catalog
from .provenance import capture_snapshot
from .simulation import derive_seed, run_batch, strategy_from_name


def source_fingerprint() -> dict[str, str]:
    root = Path(__file__).resolve().parent
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob("*.py"))}


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


@contextmanager
def benchmark_lock(path: Path):
    """OS-owned lock is released even on process exit; no stale PID guessing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Benchmark output is in use by another process") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def compact_report(report) -> dict:
    result = report.to_dict()
    # Detailed source/decision events dominate disk size. Aggregates and actual
    # paired outcomes are enough for this benchmark; ordinary simulate retains
    # its full game-detail option for mechanism tracing.
    result["games"] = [{key: game[key] for key in ("index", "seed", "status", "won")}
                       for game in result["games"]]
    return result


def merge_reports(parts: list[dict]) -> SimpleNamespace:
    """Merge counters, not per-chunk rates (including death-gap denominators)."""
    if not parts:
        raise ValueError("No completed chunks")
    offer_protocols = {p["summary"].get("offer_tracking", {}).get("protocol", "legacy_resolved_only") for p in parts}
    if len(offer_protocols) != 1:
        raise ValueError("Mismatched offer tracking; do not mix reroll exposures with legacy counters")
    config = parts[0]["config"]
    for part in parts:
        if any(part["config"][k] != config[k] for k in ("strategy", "difficulty", "fun_mode", "base_seed")):
            raise ValueError("Mismatched chunk configuration")
        if len(part["games"]) != part["summary"]["games_recorded"]:
            raise ValueError("Missing paired game records")
    games = sum(part["summary"]["games_recorded"] for part in parts)
    records = [game for part in parts for game in part["games"]]
    if len({game["seed"] for game in records}) != games:
        raise ValueError("Duplicate seeds across chunks")
    summary = {"games_requested": games, "games_recorded": games}
    for field in ("wins", "losses", "aborted", "active_choice_total", "automatic_generation_total"):
        summary[field] = sum(p["summary"].get(field, 0) for p in parts)
    summary["win_rate"] = summary["wins"] / games
    if all("offer_tracking" in p["summary"] for p in parts):
        tracking = deepcopy(parts[0]["summary"]["offer_tracking"])
        for key in ("tracked_games", "legacy_untracked_games"):
            tracking[key] = sum(p["summary"]["offer_tracking"][key] for p in parts)
        summary["offer_tracking"] = tracking
    # Missing legacy instrumentation is not a measured zero.
    for field in ("active_action_total", "active_action_games", "pool_flow_tracked_games", "pool_flow_unreconciled_games"):
        if all(field in p["summary"] for p in parts):
            summary[field] = sum(p["summary"][field] for p in parts)
    for field in ("active_action_counts", "active_action_item_counts", "bundle_choice_counts", "pool_flow_totals", "pool_removal_origin_counts"):
        if all(field in p["summary"] for p in parts):
            counts = Counter()
            for p in parts:
                counts.update(p["summary"][field])
            summary[field] = dict(counts)
    for field in ("average_final_gold", "average_spins", "average_orders_completed", "average_max_pool_size",
                  "pool_over_30_rate", "average_rolls", "average_deletes"):
        summary[field] = sum(p["summary"].get(field, 0) * p["summary"]["games_recorded"] for p in parts) / games
    for field in ("pool_size_distribution", "pool_growth_source_counts", "death_reasons"):
        counts = Counter()
        for p in parts:
            counts.update(p["summary"].get(field, {}))
        summary[field] = dict(counts)
    summary["pool_band_choice_stats"] = {}
    for band in ("under_15", "15_19", "20_plus"):
        counts = {k: sum(p["summary"]["pool_band_choice_stats"][band][k] for p in parts)
                  for k in ("choices", "selected", "skipped")}
        counts["selection_rate"] = counts["selected"] / counts["choices"] if counts["choices"] else 0.0
        summary["pool_band_choice_stats"][band] = counts
    generator = {k: sum(p["summary"]["generator_choice_stats"][k] for p in parts) for k in ("offered", "selected")}
    generator["selection_rate"] = generator["selected"] / generator["offered"] if generator["offered"] else 0.0
    for key in ("resolved_offered", "rerolled_offered", "unresolved_offered"):
        if all(key in p["summary"]["generator_choice_stats"] for p in parts):
            generator[key] = sum(p["summary"]["generator_choice_stats"][key] for p in parts)
    summary["generator_choice_stats"] = generator
    if all("generator_capability_stats" in p["summary"] for p in parts):
        summary["generator_capability_stats"] = {}
        kinds = sorted({kind for p in parts for kind in p["summary"]["generator_capability_stats"]})
        for kind in kinds:
            counts = {key: sum(p["summary"]["generator_capability_stats"].get(kind, {}).get(key, 0) for p in parts)
                      for key in ("offered", "selected", "resolved_offered", "rerolled_offered", "unresolved_offered")}
            counts["selection_rate"] = counts["selected"] / counts["offered"] if counts["offered"] else None
            summary["generator_capability_stats"][kind] = counts
    summary["order_progression"] = []
    orders = sorted({r["order"] for p in parts for r in p["summary"]["order_progression"]})
    for order in orders:
        rows = [r for p in parts for r in p["summary"]["order_progression"] if r["order"] == order]
        reached, died = sum(r["reached"] for r in rows), sum(r["died"] for r in rows)
        gap_sum = sum((r["average_gold_gap_at_death"] or 0) * r["died"] for r in rows)
        summary["order_progression"].append({"order": order, "reached": reached, "died": died,
            "conditional_death_rate": died / reached if reached else None,
            "average_gold_gap_at_death": gap_sum / died if died else None})
    content = {}
    count_fields = ("offer_count", "choice_count", "acquisition_count", "trigger_count", "triggered_games",
                    "wins_when_triggered", "consumed_count", "consumed_games", "wins_when_consumed",
                    "selected_games", "final_owned_count", "final_owned_games", "wins_when_selected", "wins_when_owned",
                    "removal_count", "removed_games", "wins_when_removed")
    for category in parts[0]["content"]:
        merged = {}
        for part in parts:
            for row in part["content"][category]:
                if row["id"] not in merged:
                    merged[row["id"]] = {k: deepcopy(v) for k, v in row.items() if k not in count_fields}
                    merged[row["id"]].update({k: 0 for k in count_fields})
                for k in count_fields:
                    merged[row["id"]][k] += int(row.get(k, 0))
        for row in merged.values():
            row["selection_rate"] = row["choice_count"] / row["offer_count"] if row["offer_count"] else 0.0
            row["game_selection_rate"] = row["selected_games"] / games
            row["possession_rate"] = row["final_owned_games"] / games
            row["suspected_balance"] = None  # Do not carry small-chunk flags into a larger run.
            for suffix, denominator in (("selected", "selected_games"), ("owned", "final_owned_games"),
                                        ("triggered", "triggered_games"), ("consumed", "consumed_games"),
                                        ("removed", "removed_games")):
                rate = row[f"wins_when_{suffix}"] / row[denominator] if row[denominator] else None
                row[f"win_rate_when_{suffix}"] = rate
                row[f"win_lift_when_{suffix}"] = None if rate is None else rate - summary["win_rate"]
        content[category] = list(merged.values())
    return SimpleNamespace(**{k: config[k] for k in ("difficulty", "strategy", "fun_mode", "base_seed")},
                           summary=summary, content=content, games_detail=records)


def comparison_markdown(report: dict) -> str:
    config = report["config"]
    lines = ["# 冻结卡表策略配对对照", "", "状态：" + ("已完成全部计划" if report.get("complete") else "阶段结果，计划尚未完成"),
             f"Seed：`{config['seed']}`；模式：`{config['fun_mode']}`。",
             f"`{config['baseline']}` → `{config['candidate']}`；每难度计划 {config['games']} 对。",
             "", "| 难度 | 完成对数 | 通关（旧→新） | 差值pp | 新增/丢失通关 | 配对精确p | 最大池（旧→新） | >30（旧→新） |",
             "|---|---:|---|---:|---|---:|---|---|"]
    for difficulty, result in report["difficulties"].items():
        a, b = result["baseline"], result["candidate"]
        lines.append(f"| D{difficulty} | {result['games']} | {a['win_rate']:.1%} → {b['win_rate']:.1%} | "
                     f"{100*result['win_delta']:+.2f} | {result['gained_wins']}/{result['lost_wins']} | "
                     f"{result['paired_exact_p']:.4f} | {a['average_max_pool_size']:.2f} → {b['average_max_pool_size']:.2f} | "
                     f"{a['pool_over_30_rate']:.1%} → {b['pool_over_30_rate']:.1%} |")
    lines += ["", "不同策略会分叉RNG路径；相同初始seed配对不意味着之后抽牌相同。p值未做多重比较校正。",
              "少于25个不一致胜负配对标记‘样本不足’，不据此宣布平衡优势；未完成全部计划时为阶段结果。",
              "成分/装备的触发计数尚未完整埋点；0不能解释为没有触发。物品触发统计也仅覆盖已埋点事件。"]
    for difficulty, result in report["difficulties"].items():
        a, b = result["baseline"], result["candidate"]
        lines += ["", f"## D{difficulty} 过程数据", "", f"证据：{result['evidence']}；平均完成订单 "
                  f"{a['average_orders_completed']:.2f} → {b['average_orders_completed']:.2f}。",
                  "", "| 指标 | 旧 | 新 |", "|---|---:|---:|"]
        for key, label in (("active_choice_total", "主动抓取总数"), ("automatic_generation_total", "自动生成总数")):
            lines.append(f"| {label} | {a[key]} | {b[key]} |")
        for key, label in (("active_action_total", "主动使用/开关次数"), ("active_action_games", "使用主动操作的局数")):
            lines.append(f"| {label} | {a.get(key, '未统计')} | {b.get(key, '未统计')} |")
        for key, label in (("initial", "初始池总数"), ("added", "边界新增"), ("removed", "边界移除"),
                           ("identity_changes", "身份转换（不扩池）"), ("final", "最终池总数")):
            left = a.get("pool_flow_totals") if a.get("pool_flow_tracked_games", 0) else None
            right = b.get("pool_flow_totals") if b.get("pool_flow_tracked_games", 0) else None
            lines.append(f"| {label} | {left.get(key, 0) if left is not None else '未统计'} | {right.get(key, 0) if right is not None else '未统计'} |")
        lines.append(f"净池覆盖旧/新：{a.get('pool_flow_tracked_games', '未统计')}/{b.get('pool_flow_tracked_games', '未统计')}局；仅覆盖被埋点的对局，不能将未覆盖部分当0。")
        lines.append("池流量按动作边界观察；同动作内生成又消失不计入，移除构筑标签不是UID级因果来源。")
        for key, label in (("choices", "整组奖励次数"), ("accepted", "整组接受次数"), ("declined", "整组放弃次数")):
            left = a.get("bundle_choice_counts")
            right = b.get("bundle_choice_counts")
            lines.append(f"| {label} | {left.get(key, 0) if left is not None else '未统计'} | {right.get(key, 0) if right is not None else '未统计'} |")
        for band in ("under_15", "15_19", "20_plus"):
            lines.append(f"| {band} 选择率 | {a['pool_band_choice_stats'][band]['selection_rate']:.1%} | "
                         f"{b['pool_band_choice_stats'][band]['selection_rate']:.1%} |")
        lines += ["", "| 订单 | reached旧/新 | died旧/新 | 条件死亡率旧/新 | 死亡缺口g旧/新 |",
                  "|---|---|---|---|---|"]
        for x, y in zip(a["order_progression"], b["order_progression"]):
            def gap(r):
                v = r["average_gold_gap_at_death"]
                return "—" if v is None else f"{v:.1f}"
            def death_rate(r):
                value = r["conditional_death_rate"]
                return "—" if value is None else f"{value:.1%}"
            lines.append(f"| {x['order']} | {x['reached']}/{y['reached']} | {x['died']}/{y['died']} | "
                         f"{death_rate(x)}/{death_rate(y)} | {gap(x)}/{gap(y)} |")
    return "\n".join(lines) + "\n"


def _run_benchmark(*, output: Path, games: int, seed: int, difficulties: list[int], baseline: str,
                  candidate: str, chunk_size: int = 50, fun_mode: str = "none", resume: bool = False,
                  max_chunks: int | None = None) -> dict:
    if games < 1 or chunk_size < 1 or not difficulties or any(not 1 <= d <= 15 for d in difficulties):
        raise ValueError("Invalid games/chunk size/difficulties")
    if len(set(difficulties)) != len(difficulties):
        raise ValueError("Duplicate difficulties")
    if max_chunks is not None and max_chunks < 0:
        raise ValueError("max_chunks must be nonnegative")
    strategy_from_name(baseline); strategy_from_name(candidate)
    catalog = Catalog.load()
    config = {"games": games, "seed": seed, "difficulties": sorted(difficulties), "baseline": baseline,
              "candidate": candidate, "chunk_size": chunk_size, "fun_mode": fun_mode}
    fingerprint = {"source": source_fingerprint(), "catalog": catalog_hash(catalog), "python": sys.version}
    directory = output.with_suffix(".chunks")
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        if not resume:
            raise ValueError("Existing benchmark; use --resume or a new output")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["config"] != config or manifest["fingerprint"] != fingerprint:
            raise ValueError("Resume refused: configuration/source/catalog changed")
    else:
        if resume or output.exists():
            raise ValueError("No matching resumable manifest, or output already exists")
        manifest = {"config": config, "fingerprint": fingerprint,
                    "snapshot": capture_snapshot(output.with_suffix(".snapshot.zip"), catalog)}
        atomic_json(manifest_path, manifest)
    report = {"protocol": "crucible-echoes-policy-benchmark/v1", **manifest, "difficulties": {}, "complete": False}
    executed = 0
    for difficulty in sorted(difficulties):
        chunks = []
        for start in range(0, games, chunk_size):
            count = min(chunk_size, games - start)
            path = directory / f"d{difficulty}_{start:06d}.json"
            expected = {"difficulty": difficulty, "start": start, "count": count}
            if path.exists():
                chunk = json.loads(path.read_text(encoding="utf-8"))
                if chunk["range"] != expected:
                    raise ValueError("Invalid checkpoint range")
            else:
                if max_chunks is not None and executed >= max_chunks:
                    break
                if source_fingerprint() != fingerprint["source"] or catalog_hash(Catalog.load()) != fingerprint["catalog"]:
                    raise ValueError("Source/catalog changed during benchmark; start a new run")
                pair = []
                for name in (baseline, candidate):
                    print(f"D{difficulty} {name}: games {start+1}..{start+count}", flush=True)
                    result = run_batch(count, seed, difficulty, strategy=strategy_from_name(name),
                                       catalog=catalog, start_index=start, fun_mode=fun_mode)
                    if result.summary["aborted"]:
                        raise ValueError("Aborted simulation; do not treat it as a balance loss")
                    pair.append(compact_report(result))
                if source_fingerprint() != fingerprint["source"] or catalog_hash(Catalog.load()) != fingerprint["catalog"]:
                    raise ValueError("Source/catalog changed during chunk; checkpoint not saved")
                chunk = {"range": expected, "baseline": pair[0], "candidate": pair[1]}
                atomic_json(path, chunk)
                executed += 1
            for key, name in (("baseline", baseline), ("candidate", candidate)):
                part = chunk[key]
                required = {"base_seed": seed, "difficulty": difficulty, "games": count,
                            "strategy": name, "fun_mode": fun_mode}
                if part["config"] != required or len(part["games"]) != count:
                    raise ValueError("Checkpoint configuration/count mismatch")
                for index, game in enumerate(part["games"], start):
                    if game["index"] != index or game["seed"] != derive_seed(seed, index):
                        raise ValueError("Checkpoint index/seed mismatch")
            chunks.append(chunk)
            result = compare(merge_reports([c["baseline"] for c in chunks]),
                             merge_reports([c["candidate"] for c in chunks]), allow_strategy_difference=True)
            report["difficulties"][str(difficulty)] = result
            atomic_json(output, report)
            output.with_suffix(".md").write_text(comparison_markdown(report), encoding="utf-8")
    report["complete"] = (len(report["difficulties"]) == len(difficulties)
                          and all(r["games"] == games for r in report["difficulties"].values()))
    atomic_json(output, report)
    output.with_suffix(".md").write_text(comparison_markdown(report), encoding="utf-8")
    return report


def run_benchmark(**kwargs) -> dict:
    output = Path(kwargs["output"])
    with benchmark_lock(output.with_suffix(".lock")):
        return _run_benchmark(**{**kwargs, "output": output})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--games", type=int, default=500)
    p.add_argument("--seed", type=int, default=20261002)
    p.add_argument("--difficulties", type=int, nargs="+", default=[7, 10, 15])
    p.add_argument("--baseline", default="heuristic-v2-content")
    p.add_argument("--candidate", default="heuristic-v2-content-v2")
    p.add_argument("--chunk-size", type=int, default=50)
    p.add_argument("--fun-mode", choices=("none", "giant", "rapid", "blind_box", "minimal", "mutation"), default="none")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--resume", action="store_true")
    args = p.parse_args()
    run_benchmark(**vars(args))


if __name__ == "__main__":
    main()
