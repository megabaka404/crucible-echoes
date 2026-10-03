"""Inspect pure item-policy counterfactuals on states actually visited by v4.

This is not an outcome A/B experiment. It observes resolved item choices only,
not all rerolled exposures; no game data or default policy is changed.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from crucible_echoes.action_strategy import ContentActionStrategy
from crucible_echoes.balance_experiment import catalog_hash
from crucible_echoes.catalog import Catalog
from crucible_echoes.item_strategy import ITEM_ECONOMY_FIELDS, ItemEconomyStrategy
from crucible_echoes.provenance import capture_snapshot
from crucible_echoes.simulation import run_batch
from crucible_echoes.strategy_benchmark import atomic_json, benchmark_lock, source_fingerprint


def selector_count(engine, event):
    if event.startswith("removed_tag:"):
        tag = event.split(":", 1)[1]
        return sum(tag in engine.catalog.ingredients[x.def_id].get("tags", []) for x in engine.s.ingredients)
    if event.startswith("removed_ids:"):
        ids = set(event.split(":", 1)[1].split(","))
        return sum(x.def_id in ids for x in engine.s.ingredients)
    return None  # Do not guess targets for opaque event names.


def token_reward_types(row):
    types = set()
    for field in ("per_spin_token", "periodic_token"):
        rule = row.get(field, {})
        if rule.get("token") and rule.get("amount", 1) > 0:
            types.add(rule["token"])
    for rule in row.get("event_bonus_every", {}).values():
        types.update(token for token, amount in rule.get("tokens", {}).items() if amount > 0)
    return sorted(types)


class ObservedItemPolicy(ItemEconomyStrategy):
    def __init__(self):
        super().__init__()
        self.baseline = ContentActionStrategy()
        self.traces = []

    def choose(self, engine, choice):
        if choice.kind != "item":
            return super().choose(engine, choice)
        before = deepcopy(engine.s.to_dict())
        rows = []
        for def_id in choice.offers:
            row = engine.catalog.items[def_id]
            events = set(row.get("event_bonus", {})) | set(row.get("event_bonus_every", {}))
            if row.get("order_savings"):
                events.add("order_completed")
            rows.append({"id": def_id, "baseline_score": self.baseline.score(engine, "item", def_id),
                         "candidate_score": self.score(engine, "item", def_id),
                         "economy_components": self.item_economy_components(engine, row),
                         "economy_fields": [field for field in ITEM_ECONOMY_FIELDS if row.get(field)],
                         "token_reward_types": token_reward_types(row),
                         "events": {event: {"historical_per_spin": self.event_rate(engine, event),
                                            "current_selector_count": selector_count(engine, event)}
                                    for event in sorted(events)}})
        old = self.baseline.choose(engine, choice)
        old_reroll = self.baseline.should_reroll(engine, choice)
        selected = super().choose(engine, choice)
        if engine.s.to_dict() != before:
            raise AssertionError("Item diagnostics changed state/RNG")
        self.traces.append({"seed": engine.s.seed, "spin": engine.s.spin,
                            "pool_size": len(engine.s.ingredients), "source": choice.source,
                            "tokens": dict(engine.s.tokens), "spins_left": engine.s.spins_left,
                            "selected": choice.offers[selected - 1] if selected else None,
                            "baseline_recommendation": choice.offers[old - 1] if old else None,
                            "baseline_would_reroll": old_reroll, "offers": rows})
        return selected


def token_usage_summary(batch):
    """Actual action counts and final stock, not invented gross token gains.

    Essence choices originate in several ways, so they are not labelled as
    Essence Token spending. No current recorder exposes all per-type gains.
    """
    rows = []
    for game in batch.games_detail:
        events = game.get("strategy_events", {})
        rows.append({"seed": game["seed"], "spins": game["spins"], "status": game["status"],
                     "final_tokens": dict(game["final_attributes"]["tokens"]),
                     "roll_actions": len(events.get("rolls", [])),
                     "delete_actions": len(events.get("deletes", [])),
                     "essence_choices": sum(x.get("kind") == "essence" for x in events.get("choices", []))})
    tokens = sorted({token for row in rows for token in row["final_tokens"]})
    return {"games": rows,
            "totals": {"roll_actions": sum(x["roll_actions"] for x in rows),
                       "delete_actions": sum(x["delete_actions"] for x in rows),
                       "essence_choices": sum(x["essence_choices"] for x in rows),
                       "final_tokens": {token: sum(x["final_tokens"].get(token, 0) for x in rows) for token in tokens}},
            "coverage": "Actual Roll/Delete actions and final stock. Essence choices are not per-type spending; gross gains and future marginal utility remain untracked."}


def run_diagnostics(*, games, seed, difficulties, output):
    if games < 1 or not difficulties or len(set(difficulties)) != len(difficulties) or any(not 1 <= d <= 15 for d in difficulties):
        raise ValueError("Invalid games/difficulties")
    output = Path(output)
    catalog = Catalog.load()
    script = Path(__file__).read_bytes()
    fingerprint = {"source": source_fingerprint(), "catalog": catalog_hash(catalog),
                   "python": sys.version, "analysis": hashlib.sha256(script).hexdigest()}
    def frozen():
        return (source_fingerprint() == fingerprint["source"] and catalog_hash(Catalog.load()) == fingerprint["catalog"]
                and hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == fingerprint["analysis"])
    with benchmark_lock(output.with_suffix(".lock")):
        if output.exists():
            raise ValueError("Existing diagnostics; choose a new output")
        report = {"protocol": "crucible-echoes-item-decisions/v2", "complete": False,
                  "config": {"games": games, "seed": seed, "difficulties": sorted(difficulties), "fun_mode": "none"},
                  "fingerprint": fingerprint, "analysis_source": script.decode("utf-8"),
                  "snapshot": capture_snapshot(output.with_suffix(".snapshot.zip"), catalog), "difficulties": {},
                  "scope": "Resolved item decisions on v4 trajectories, not reroll exposures or causal outcome comparisons.",
                  "caveat": "Empty current selector can be replenished by future generation. Historical events are not forecasts."}
        atomic_json(output, report)
        for difficulty in sorted(difficulties):
            if not frozen():
                raise ValueError("Source/catalog changed")
            policy = ObservedItemPolicy()
            batch = run_batch(games, seed, difficulty, strategy=policy, catalog=catalog)
            if batch.summary["aborted"]:
                raise ValueError("Aborted diagnostics are not valid losses")
            if not frozen():
                raise ValueError("Source/catalog changed during diagnostics")
            offers = [offer for trace in policy.traces for offer in trace["offers"]]
            empty_history = sum(any(event["historical_per_spin"] > 0 and event["current_selector_count"] == 0
                                    for event in offer["events"].values()) for offer in offers)
            report["difficulties"][str(difficulty)] = {
                "games": games, "aborted": batch.summary["aborted"], "decisions": len(policy.traces),
                "recommendation_changes": sum(t["selected"] != t["baseline_recommendation"] for t in policy.traces),
                "candidate_slots": len(offers), "positive_history_empty_selector_slots": empty_history,
                "token_usage": token_usage_summary(batch),
                "traces": policy.traces}
            atomic_json(output, report)
        report["complete"] = True
        atomic_json(output, report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", type=int, default=40)
    parser.add_argument("--seed", type=int, default=20261017)
    parser.add_argument("--difficulties", type=int, nargs="+", default=[7, 15])
    parser.add_argument("--output", type=Path, required=True)
    run_diagnostics(**vars(parser.parse_args()))


if __name__ == "__main__":
    main()
