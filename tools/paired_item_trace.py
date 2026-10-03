"""Replay discordant paired games from their exact archived source package.

Only resolved item decisions are traced. Differing trajectories/rerolls are
not attributed to one item, and this tool never changes game data or policy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time


def publish_report(path, report, writer):
    """Bound transient Windows sharing errors, without rerunning games."""
    for attempt in range(5):
        try:
            writer(path, report)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.01)


def discordant_pairs(chunks, difficulty):
    pairs = []
    seen = set()
    for chunk in chunks:
        if chunk['range']['difficulty'] != difficulty:
            continue
        left, right = chunk['baseline']['games'], chunk['candidate']['games']
        if len(left) != len(right) or len(left) != chunk['range']['count']:
            raise ValueError('Paired checkpoint count mismatch')
        for baseline, candidate in zip(left, right):
            identity = (baseline['index'], baseline['seed'])
            if identity != (candidate['index'], candidate['seed']) or identity in seen:
                raise ValueError('Mismatched or duplicated paired identity')
            seen.add(identity)
            if baseline['status'] == 'aborted' or candidate['status'] == 'aborted':
                raise ValueError('Aborted games are not valid paired outcomes')
            if baseline['won'] != candidate['won']:
                pairs.append({'index': identity[0], 'seed': identity[1],
                              'baseline_won': baseline['won'], 'candidate_won': candidate['won']})
    return sorted(pairs, key=lambda pair: pair['index'])


def common_decision_changes(left, right):
    """Compare only exactly identical observable states and final candidates."""
    changes = []
    contexts = {row['state_hash']: row for row in left}
    for row in right:
        old = contexts.get(row['state_hash'])
        if old is not None and old['selected'] != row['selected']:
            changes.append({'spin': row['spin'], 'order': row['order'],
                            'baseline_selected': old['selected'], 'candidate_selected': row['selected'],
                            'offers': row['offers']})
    return changes


def observe_before_commit(policy, observer):
    """Observe the final strategy decision before the core executes it.

    simulate_game's on_choice callback is post-commit, so it cannot supply
    the decision state. Preserve the original return value and all other
    policy methods; do not call choose twice or manufacture extra actions.
    """
    original = policy.choose
    def choose(engine, choice):
        index = original(engine, choice)
        actual = index if index is not None else (None if choice.can_skip else 1)
        selected = choice.offers[actual - 1] if actual is not None else None
        observer(engine, choice, selected)
        return index
    policy.choose = choose


def observe_reroll_before_commit(policy, observer):
    original = policy.should_reroll
    def should_reroll(engine, choice):
        decision = original(engine, choice)
        observer(engine, choice, decision)
        return decision
    policy.should_reroll = should_reroll


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--benchmark', type=Path, required=True)
    parser.add_argument('--package-root', type=Path, required=True)
    parser.add_argument('--difficulty', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Choose a new output; existing analysis is preserved')
    benchmark = json.loads(args.benchmark.read_text(encoding='utf-8'))
    if not benchmark.get('complete'):
        raise ValueError('Only completed benchmarks may be analysed')
    package = args.package_root.resolve(strict=True) / 'crucible_echoes'
    expected = benchmark['fingerprint']['source']
    actual = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in package.glob('*.py')}
    if actual != expected:
        raise ValueError('Archived source fingerprint differs from the benchmark')
    # Import the explicitly verified archive, not the current checkout.
    sys.path.insert(0, str(package.parent))
    from crucible_echoes.balance_experiment import catalog_hash
    from crucible_echoes.catalog import Catalog
    from crucible_echoes.item_strategy import ItemEconomyStrategy
    from crucible_echoes.simulation import simulate_game, strategy_from_name
    from crucible_echoes.strategy_benchmark import atomic_json
    catalog = Catalog.load()
    if catalog_hash(catalog) != benchmark['fingerprint']['catalog']:
        raise ValueError('Archived catalog differs from the benchmark')
    chunks = [json.loads(path.read_text(encoding='utf-8')) for path in sorted(
        args.benchmark.with_suffix('.chunks').glob(f'd{args.difficulty}_*.json'))]
    pairs = discordant_pairs(chunks, args.difficulty)
    expected_pairs = benchmark['difficulties'][str(args.difficulty)]['discordant_pairs']
    if len(pairs) != expected_pairs:
        raise ValueError('Missing discordant checkpoint games')
    report = {'protocol': 'crucible-echoes-paired-item-trace/v3', 'complete': False,
              'benchmark': str(args.benchmark), 'fingerprint': benchmark['fingerprint'],
              'difficulty': args.difficulty, 'analysis_source': Path(__file__).read_text(encoding='utf-8'),
              'scope': 'Discordant games only; item picks and reroll decisions observed before core commit. Identical-state decision changes are observations, not individual-item causal proofs.',
              'planned_pairs': len(pairs), 'pairs': []}
    publish_report(args.output, report, atomic_json)
    economy = ItemEconomyStrategy()
    for pair in pairs:
        trajectories = []
        reroll_trajectories = []
        for side in ('baseline', 'candidate'):
            policy = strategy_from_name(benchmark['config'][side])
            traces = []
            reroll_traces = []
            def capture(engine, choice, selected, destination):
                if choice.kind != 'item':
                    return
                state = {'state': engine.s.to_dict(), 'rng': engine.r.state}
                before = json.dumps(state, ensure_ascii=False, sort_keys=True)
                offers = [{'id': key, 'name': catalog.items[key]['name'],
                           'score': policy.score(engine, 'item', key),
                           'economy_components': economy.item_economy_components(engine, catalog.items[key])}
                          for key in choice.offers]
                after = json.dumps({'state': engine.s.to_dict(), 'rng': engine.r.state}, ensure_ascii=False, sort_keys=True)
                if before != after:
                    raise AssertionError('Decision diagnostics changed engine state/RNG')
                destination.append({'state_hash': hashlib.sha256(before.encode()).hexdigest(),
                               'spin': engine.s.spin, 'order': engine.s.order_index + 1,
                               'selected': selected, 'offers': offers})
            observe_before_commit(policy, lambda e, c, key: capture(e, c, key, traces))
            observe_reroll_before_commit(policy, lambda e, c, decision: capture(e, c, decision, reroll_traces))
            record = simulate_game(pair['seed'], args.difficulty, strategy=policy,
                                   game_index=pair['index'], fun_mode=benchmark['config']['fun_mode'],
                                   catalog=catalog)
            if record.status == 'aborted' or record.won != pair[f'{side}_won']:
                raise ValueError('Archived outcome replay did not match; analysis refused')
            trajectories.append(traces)
            reroll_trajectories.append(reroll_traces)
        report['pairs'].append({**pair, 'common_state_changes': common_decision_changes(*trajectories),
                                'common_reroll_changes': common_decision_changes(*reroll_trajectories),
                                'baseline_traces': trajectories[0], 'candidate_traces': trajectories[1],
                                'baseline_reroll_traces': reroll_trajectories[0],
                                'candidate_reroll_traces': reroll_trajectories[1]})
        publish_report(args.output, report, atomic_json)
        print(f"D{args.difficulty}: replayed {len(report['pairs'])}/{len(pairs)} pairs", flush=True)
    report['complete'] = True
    publish_report(args.output, report, atomic_json)


if __name__ == '__main__':
    main()
