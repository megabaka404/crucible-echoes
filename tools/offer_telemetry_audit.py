"""Audit candidate-slot conservation without changing any strategy/game rule.

python -m tools.offer_telemetry_audit --games 100 --seed 20261012
--difficulties 7 15 --output reports/offer_audit.json
"""
from collections import Counter
import argparse
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from crucible_echoes.balance_experiment import catalog_hash
from crucible_echoes.catalog import Catalog
from crucible_echoes.generation_analysis import GENERATION_CLASSES, generation_classes
from crucible_echoes.provenance import capture_snapshot
from crucible_echoes.simulation import run_batch, strategy_from_name
from crucible_echoes.strategy_benchmark import atomic_json, benchmark_lock, source_fingerprint


def audit_report(report, catalog):
    """Reconcile raw slots with per-game and aggregate counters independently."""
    if len(report.games_detail) != report.games or report.summary["aborted"]:
        raise ValueError("Audit requires every game detail and zero aborted games")
    total, resolved, selected = Counter(), Counter(), Counter()
    outcomes, duplicate_slots = Counter(), 0
    capabilities = {kind: Counter() for kind in GENERATION_CLASSES}

    def category(kind, def_id):
        if kind == "ingredient":
            return "equipment" if "equipment" in catalog.ingredients[def_id].get("tags", []) else "ingredients"
        return {"item": "items", "essence": "essences"}.get(kind)

    for game in report.games_detail:
        events = game["strategy_events"]
        if events.get("offer_tracking") != "candidate_slots_including_rerolls/v1":
            raise ValueError("Legacy offer counters are not complete reroll exposures")
        observed, final = Counter(), Counter()
        for exposure in events["offer_exposures"]:
            ids = exposure["offer_ids"]
            outcomes[exposure["outcome"]] += len(ids)
            duplicate_slots += len(ids) - len(set(ids))
            for def_id in ids:
                key = (category(exposure["kind"], def_id), def_id)
                observed[key] += 1
                if exposure["outcome"] == "resolved":
                    resolved[key] += 1
                    final[key] += 1
                if exposure["kind"] == "ingredient":
                    for kind in generation_classes(catalog.ingredients[def_id]):
                        capabilities[kind]["offered"] += 1
                        capabilities[kind][exposure["outcome"] + "_offered"] += 1
        reported = Counter({(kind, def_id): stats["offer_count"]
                            for kind, rows in game["content_stats"].items() for def_id, stats in rows.items()})
        if observed != reported:
            raise ValueError("Per-game candidate slots do not reconcile")
        final_events = Counter()
        for event in events["choices"]:
            kind = event["kind"]
            if kind not in {"ingredient", "item", "essence"}:
                continue
            for def_id in event["offer_ids"]:
                final_events[(category(kind, def_id), def_id)] += 1
            if event["selected"] is not None:
                selected[(category(kind, event["selected"]), event["selected"])] += 1
                if kind == "ingredient":
                    for generation_kind in generation_classes(catalog.ingredients[event["selected"]]):
                        capabilities[generation_kind]["selected"] += 1
        if final != final_events:
            raise ValueError("Resolved exposures do not reconcile with choices")
        total.update(observed)
    reported = Counter({(kind, row["id"]): row["offer_count"]
                        for kind, rows in report.content.items() for row in rows})
    chosen = Counter({(kind, row["id"]): row["choice_count"]
                      for kind, rows in report.content.items() for row in rows})
    if total != reported or selected != chosen:
        raise ValueError("Aggregate candidate/selection counters do not reconcile")
    for kind, counts in capabilities.items():
        actual = report.summary["generator_capability_stats"][kind]
        if any(counts[key] != actual[key] for key in
               ("offered", "selected", "resolved_offered", "rerolled_offered", "unresolved_offered")):
            raise ValueError("Generation capability counters do not reconcile")
    if report.summary["pool_flow_tracked_games"] != report.games or report.summary["pool_flow_unreconciled_games"]:
        raise ValueError("Pool boundary flow does not reconcile")
    rows = []
    for (kind, def_id), offers in total.items():
        final_offers, choices = resolved[(kind, def_id)], selected[(kind, def_id)]
        rows.append({"category": kind, "id": def_id, "offers": offers, "resolved_offers": final_offers,
                     "discarded_offers": offers - final_offers, "choices": choices,
                     "slot_selection_rate": choices / offers,
                     "resolved_slot_selection_rate": choices / final_offers if final_offers else None})
    rows.sort(key=lambda r: (-r["discarded_offers"], r["category"], r["id"]))
    return {"games": report.games, "wins": report.summary["wins"], "reconciled": True,
            "candidate_slots": sum(total.values()), "resolved_slots": sum(resolved.values()),
            "outcome_slots": dict(outcomes), "duplicate_slots": duplicate_slots, "content": rows,
            "generator_capability_stats": report.summary["generator_capability_stats"],
            "pool_flow_totals": report.summary["pool_flow_totals"],
            "games_detail": [{key: game[key] for key in ("index", "seed", "status", "won")} for game in report.games_detail]}


def run_audit(*, games, seed, difficulties, output):
    output = Path(output)
    if games < 1 or not difficulties or len(set(difficulties)) != len(difficulties) or any(not 1 <= d <= 15 for d in difficulties):
        raise ValueError("Invalid games/difficulties")
    catalog = Catalog.load()
    if catalog.validate():
        raise ValueError("Invalid catalog")
    fingerprint = {"source": source_fingerprint(), "catalog": catalog_hash(catalog), "python": sys.version,
                   "analysis": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    def frozen():
        return fingerprint == {"source": source_fingerprint(), "catalog": catalog_hash(Catalog.load()), "python": sys.version,
                               "analysis": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    with benchmark_lock(output.with_suffix(".lock")):
        if output.exists():
            raise ValueError("Choose a new output path; audit already exists")
        report = {"protocol": "crucible-echoes-offer-audit/v1", "complete": False,
                  "config": {"games": games, "seed": seed, "difficulties": sorted(difficulties),
                             "strategy": "heuristic-v2-content-v3", "fun_mode": "none"},
                  "fingerprint": fingerprint, "snapshot": capture_snapshot(output.with_suffix(".snapshot.zip"), catalog),
                  "note": "同一批对局的统计口径核对，不是策略/卡牌改动A/B；不据此调数值。", "difficulties": {}}
        atomic_json(output, report)
        for difficulty in sorted(difficulties):
            if not frozen():
                raise ValueError("Frozen source/catalog changed")
            print(f"D{difficulty}: {games} games", flush=True)
            batch = run_batch(games, seed, difficulty, catalog=catalog, strategy=strategy_from_name("heuristic-v2-content-v3"))
            result = audit_report(batch, catalog)
            if not frozen():
                raise ValueError("Frozen source/catalog changed; results not saved")
            report["difficulties"][str(difficulty)] = result
            atomic_json(output, report)
        report["complete"] = True
        atomic_json(output, report)
        print("All exposure counters reconciled", flush=True)
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20261012)
    parser.add_argument("--difficulties", type=int, nargs="+", default=[7, 15])
    parser.add_argument("--output", required=True)
    run_audit(**vars(parser.parse_args()))
