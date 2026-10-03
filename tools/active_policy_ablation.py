"""Controlled policy-only ablation; no card data or game rules are changed.

Example: python -m tools.active_policy_ablation --field order_book --games 100
--seed 20261007 --difficulties 7 10 15 --output reports/order_book_ablation.json
"""
from __future__ import annotations

import argparse
from copy import copy, deepcopy
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from crucible_echoes.action_strategy import ContentActionStrategy
from crucible_echoes.balance_experiment import catalog_hash, compare
from crucible_echoes.catalog import Catalog
from crucible_echoes.provenance import capture_snapshot
from crucible_echoes.simulation import run_batch
from crucible_echoes.strategy_benchmark import atomic_json, benchmark_lock, comparison_markdown, source_fingerprint


class SuppressedActivePolicy(ContentActionStrategy):
    """Hide one effect field only from the pure active-action decision view.

    Actual engine state, scoring, item acquisition and operation validation
    still use the original catalog. Other active effects remain available.
    The treatment is an effect capability, never a specific item ID.
    """

    def __init__(self, field: str):
        super().__init__()
        self.field = field
        self.name = f"heuristic-v2-content-v3-no-{field}"
        self._original_catalog = None
        self._decision_catalog = None

    def pre_spin_action(self, engine):
        if self._original_catalog is not engine.catalog:
            self._original_catalog = engine.catalog
            self._decision_catalog = deepcopy(engine.catalog)
            for row in self._decision_catalog.items.values():
                if self.field in row.get("active", {}):
                    row["active"].pop(self.field)
        decision_view = copy(engine)
        decision_view.catalog = self._decision_catalog
        return super().pre_spin_action(decision_view)


def run_experiment(*, field, games, seed, difficulties, output):
    output = Path(output)
    if games < 1 or not difficulties or len(set(difficulties)) != len(difficulties) or any(not 1 <= d <= 15 for d in difficulties):
        raise ValueError("Invalid games/difficulties")
    catalog = Catalog.load()
    if not any(field in row.get("active", {}) for row in catalog.items.values()):
        raise ValueError("Unknown active effect field (not an item ID)")
    source = Path(__file__).read_bytes()
    fingerprint = {"source": source_fingerprint(), "catalog": catalog_hash(catalog), "python": sys.version,
                   "analysis": hashlib.sha256(source).hexdigest()}

    def frozen():
        return (source_fingerprint() == fingerprint["source"] and catalog_hash(Catalog.load()) == fingerprint["catalog"]
                and hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == fingerprint["analysis"])

    with benchmark_lock(output.with_suffix(".lock")):
        if output.exists():
            raise ValueError("Existing analysis; choose a new output path")
        report = {"protocol": "crucible-echoes-active-policy-ablation/v1", "complete": False,
                  "config": {"games": games, "seed": seed, "difficulties": sorted(difficulties), "fun_mode": "none",
                             "baseline": ContentActionStrategy.name, "candidate": SuppressedActivePolicy(field).name},
                  "field": field, "fingerprint": fingerprint, "analysis_source": source.decode("utf-8"),
                  "snapshot": capture_snapshot(output.with_suffix(".snapshot.zip"), catalog), "difficulties": {},
                  "note": "策略操作消融，不是移除卡牌能力或修改数值；相同初始seed，后续随机路径可分叉。"}
        for difficulty in sorted(difficulties):
            if not frozen():
                raise ValueError("Frozen source/catalog changed; do not combine versions")
            pair = []
            for policy in (ContentActionStrategy(), SuppressedActivePolicy(field)):
                print(f"D{difficulty} {policy.name}: {games} games", flush=True)
                result = run_batch(games, seed, difficulty, strategy=policy, catalog=catalog)
                if result.summary["aborted"]:
                    raise ValueError("Aborted simulations cannot be classified as losses")
                pair.append(result)
            if not frozen():
                raise ValueError("Frozen source/catalog changed; result not saved")
            report["difficulties"][str(difficulty)] = compare(*pair, allow_strategy_difference=True)
            atomic_json(output, report)
            output.with_suffix(".md").write_text(comparison_markdown(report), encoding="utf-8")
        report["complete"] = True
        atomic_json(output, report)
        output.with_suffix(".md").write_text(comparison_markdown(report), encoding="utf-8")
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--field", required=True)
    parser.add_argument("--games", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20261007)
    parser.add_argument("--difficulties", nargs="+", type=int, default=[7, 10, 15])
    parser.add_argument("--output", type=Path, required=True)
    run_experiment(**vars(parser.parse_args()))


if __name__ == "__main__":
    main()
