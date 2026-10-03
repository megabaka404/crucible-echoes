"""Paired numerical experiments without modifying the live card catalog.

Run with ``python -m crucible_echoes.balance_experiment --help``.
Patches are JSON lists of {collection, id, path, value}; path is a list of
existing dictionary keys. Difficulty/order rules cannot be patched here.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path

from .catalog import Catalog
from .provenance import capture_snapshot
from .simulation import run_batch, strategy_from_name


def catalog_hash(catalog: Catalog) -> str:
    payload = json.dumps(asdict(catalog), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def paired_exact_p(gained: int, lost: int) -> float:
    """Two-sided exact McNemar/binomial test of discordant paired wins."""
    if gained < 0 or lost < 0:
        raise ValueError("Discordant counts must be nonnegative")
    n = gained + lost
    if not n:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(gained, lost) + 1))
    return min(1.0, 2 * tail / (1 << n))


def wilson_interval(wins: int, games: int) -> list[float] | None:
    if games < 0 or not 0 <= wins <= games:
        raise ValueError("Invalid binomial counts")
    if not games:
        return None
    z = 1.96
    rate = wins / games
    denom = 1 + z * z / games
    center = (rate + z * z / (2 * games)) / denom
    margin = z * math.sqrt(rate * (1 - rate) / games + z * z / (4 * games * games)) / denom
    return [max(0.0, center - margin), min(1.0, center + margin)]


def patched_catalog(original: Catalog, patches: list[dict]) -> Catalog:
    result = deepcopy(original)
    for patch in patches:
        if patch["collection"] not in {"ingredients", "items", "essences"}:
            raise ValueError("Only card collections may be patched")
        target = getattr(result, patch["collection"])[patch["id"]]
        path = patch["path"]
        if not isinstance(path, list) or not path:
            raise ValueError("path must be a nonempty list of keys")
        for key in path[:-1]:
            target = target[key]
        old, new = target[path[-1]], patch["value"]
        if any(isinstance(v, bool) or not isinstance(v, (int, float))
               or not math.isfinite(v) for v in (old, new)):
            raise ValueError("Only finite numerical fields may be patched")
        if isinstance(old, int) and not isinstance(new, int):
            raise ValueError("Integer fields must remain integers")
        target[path[-1]] = new
    return result


def compare(baseline, candidate, *, allow_strategy_difference: bool = False) -> dict:
    for field in ("difficulty", "strategy", "fun_mode", "base_seed"):
        if field == "strategy" and allow_strategy_difference:
            continue
        if getattr(baseline, field, None) != getattr(candidate, field, None):
            raise ValueError(f"Comparison configuration mismatch: {field}")
    if not baseline.games_detail or len(baseline.games_detail) != len(candidate.games_detail):
        raise ValueError("Paired comparison requires equal, nonempty game records")
    pairs = list(zip(baseline.games_detail, candidate.games_detail))
    if any(a["seed"] != b["seed"] for a, b in pairs):
        raise ValueError("Seeds do not match")
    if len({a["seed"] for a, _ in pairs}) != len(pairs):
        raise ValueError("Duplicate seeds are not independent samples")
    if any(a.get("status") == "aborted" or b.get("status") == "aborted" for a, b in pairs):
        raise ValueError("Aborted simulations cannot be treated as balance losses")
    gains = sum(not a["won"] and b["won"] for a, b in pairs)
    losses = sum(a["won"] and not b["won"] for a, b in pairs)
    n = len(pairs)
    delta = (gains - losses) / n
    # Paired standard error, not two independent binomial standard errors.
    variance = ((gains + losses) - n * delta ** 2) / max(1, n - 1)
    margin = 1.96 * math.sqrt(max(0.0, variance) / n)
    return {
        "comparison_kind": "strategy" if allow_strategy_difference else "numerical",
        "baseline_strategy": getattr(baseline, "strategy", None),
        "candidate_strategy": getattr(candidate, "strategy", None),
        "games": n, "gained_wins": gains, "lost_wins": losses,
        "win_delta": delta,
        "approximate_paired_95_interval": (
            [max(-1, delta-margin), min(1, delta+margin)] if gains + losses >= 25 else None
        ),
        "discordant_pairs": gains + losses,
        "paired_exact_p": paired_exact_p(gains, losses),
        "baseline_win_95_interval": wilson_interval(sum(bool(a["won"]) for a, _ in pairs), n),
        "candidate_win_95_interval": wilson_interval(sum(bool(b["won"]) for _, b in pairs), n),
        "evidence": "样本不足" if gains + losses < 25 else "需结合多seed复核",
        "baseline": baseline.summary, "candidate": candidate.summary,
        "content_before": baseline.content, "content_after": candidate.content,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--games", type=int, default=500)
    parser.add_argument("--seed", type=int, default=20260920)
    parser.add_argument("--difficulties", type=int, nargs="+", default=[7, 10, 15])
    parser.add_argument("--strategy", default="heuristic-v2")
    parser.add_argument("--candidate-strategy", help="Explicit strategy comparison; must use an empty patch list")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if any(not 1 <= d <= 15 for d in args.difficulties):
        parser.error("difficulties must be in 1..15")
    patches = json.loads(args.patch.read_text(encoding="utf-8"))
    if args.candidate_strategy and patches:
        parser.error("Do not combine card changes and policy changes in one comparison")
    baseline = Catalog.load()
    candidate = patched_catalog(baseline, patches)
    report = {"seed": args.seed, "strategy": args.strategy, "patches": patches,
              "snapshot": capture_snapshot(args.output.with_suffix(".snapshot.zip"), baseline),
              "baseline_hash": catalog_hash(baseline), "candidate_hash": catalog_hash(candidate),
              "source_hashes": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                for name in ("engine.py", "simulation.py", "balance_experiment.py")},
              "difficulties": {}, "note": "相同初始seed；随机路径可能分歧。区间为近似值，不表示因果机制已证实。"}
    for difficulty in args.difficulties:
        results = []
        for name, catalog in (("baseline", baseline), ("candidate", candidate)):
            print(f"D{difficulty} {name}: {args.games} games", flush=True)
            policy_name = (args.candidate_strategy or args.strategy) if name == "candidate" else args.strategy
            results.append(run_batch(args.games, args.seed, difficulty,
                                     strategy=strategy_from_name(policy_name), catalog=catalog))
        report["difficulties"][str(difficulty)] = compare(*results, allow_strategy_difference=bool(args.candidate_strategy))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(args.output), flush=True)


if __name__ == "__main__":
    main()
