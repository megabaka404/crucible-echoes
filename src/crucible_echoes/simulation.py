from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .catalog import Catalog
from .engine import GameEngine, GameError
from .model import GameState, PendingChoice


POOL_GROWTH_SOURCES = (
    "active_choice",
    "automatic_generation",
    "copy",
    "item_generation",
    "periodic_slag",
    "other",
)


MASK64 = (1 << 64) - 1


class SimulationStrategy:
    """Interface for deterministic, explainable candidate selection."""

    name = "base"

    def choose(self, engine: GameEngine, choice: PendingChoice) -> int | None:
        raise NotImplementedError

    def should_reroll(self, engine: GameEngine, choice: PendingChoice) -> bool:
        return False

    def removal_index(self, engine: GameEngine) -> int | None:
        return None

    def score_components(self, engine: GameEngine, kind: str, def_id: str) -> dict[str, float]:
        return {"total": float(self.score(engine, kind, def_id))}


class HeuristicStrategy(SimulationStrategy):
    """A deliberately simple strategy that prefers useful, low-risk content.

    The strategy never draws its own random numbers.  It scores visible
    definition fields, long-term effects and current tag synergies, then
    chooses the highest score with the definition id as a stable tie-breaker.
    It has a soft ingredient-pool target of 25 and a hard target of 30: once
    the pool is large it skips weak offers and removes the weakest removable
    ingredient when a Delete Token is available.  This is deliberately
    generic and leaves the strategy easy to replace in a later balance pass.
    """

    name = "heuristic-v1"

    _GENERATOR_FIELDS = ("periodic_spawn", "chance_spawn", "spawn_each_spin")
    _RELEASE_FIELDS = ("remove_after", "transform_after", "growth_after", "on_removed")
    _NON_EFFECT_FIELDS = {
        "id", "name", "rarity", "base", "tags", "description", "offerable", "removable",
    }

    def __init__(self) -> None:
        self._build_state_cache_key: tuple[tuple[str, str], ...] | None = None
        self._build_state_cache: dict[str, Any] | None = None

    def _tags_for(self, engine: GameEngine, def_id: str) -> set[str]:
        return set(engine.catalog.ingredients.get(def_id, {}).get("tags", []))

    def _effect_keys(self, row: dict[str, Any]) -> set[str]:
        return {
            key for key, value in row.items()
            if value and key not in self._NON_EFFECT_FIELDS
        }

    def build_state(self, engine: GameEngine) -> dict[str, Any]:
        """Return a generic, data-derived snapshot of the current build.

        Archetypes are deliberately represented by observed tags and effect
        signatures rather than a fixed list of named decks.  This makes the
        same logic work for newly added content without hidden ID bonuses.
        """
        cache_key = tuple(
            (instance.def_id, str(instance.flags.get("_sim_origin", "unknown")))
            for instance in engine.s.ingredients
        )
        if cache_key == self._build_state_cache_key and self._build_state_cache is not None:
            return self._build_state_cache
        tag_counts: Counter[str] = Counter()
        origins: Counter[str] = Counter()
        generator_tags: Counter[str] = Counter()
        mechanism_tags: Counter[str] = Counter()
        sink_tags: Counter[str] = Counter()
        for instance in engine.s.ingredients:
            definition = engine.catalog.ingredients.get(instance.def_id, {})
            tags = definition.get("tags", [])
            tag_counts.update(tags)
            if self._effect_keys(definition):
                mechanism_tags.update(tags)
            origins[str(instance.flags.get("_sim_origin", "unknown"))] += 1
            for field in self._GENERATOR_FIELDS:
                spec = definition.get(field)
                if not isinstance(spec, dict):
                    continue
                if spec.get("tag"):
                    generator_tags[str(spec["tag"])] += 1
                if spec.get("id"):
                    generator_tags.update(self._tags_for(engine, str(spec["id"])))
            value_spec = definition.get("value")
            if isinstance(value_spec, dict):
                for key in ("if_adjacent_tag", "count_tag"):
                    if value_spec.get(key):
                        sink_tags[str(value_spec[key])] += 1
                for target_id in value_spec.get("if_adjacent_ids", []) or []:
                    sink_tags.update(self._tags_for(engine, str(target_id)))
            aura = definition.get("aura")
            if isinstance(aura, dict):
                for key in ("tag", "target_tag", "count_tag"):
                    if aura.get(key):
                        sink_tags[str(aura[key])] += 1
        primary = [tag for tag, _ in tag_counts.most_common(3)]
        core = [tag for tag, count in tag_counts.items() if count >= 2]
        mechanism = sorted(set(generator_tags) | set(mechanism_tags))
        state = {
            "pool_size": len(engine.s.ingredients),
            "tag_counts": dict(tag_counts),
            "primary_tags": primary,
            "core_tags": sorted(core),
            "generator_tags": dict(generator_tags),
            "mechanism_tags": mechanism,
            "sink_tags": sorted(sink_tags),
            "origin_counts": dict(origins),
            "generator_count": sum(generator_tags.values()),
        }
        self._build_state_cache_key = cache_key
        self._build_state_cache = state
        return state

    def _pool_pressure(self, pool_size: int) -> float:
        # Pressure rises gradually and is not a hard cap.  The provenance
        # multiplier below decides whether an individual entry deserves the
        # full pressure cost.
        return max(0.0, (pool_size - 20) * 0.35)

    def _release_factor(self, row: dict[str, Any]) -> float:
        if row.get("potion") or row.get("remove_after"):
            return 0.08
        if any(row.get(field) for field in self._RELEASE_FIELDS):
            if row.get("transform_after") or row.get("growth_after"):
                return 0.45
            if row.get("on_removed"):
                return 0.65
        return 1.0

    def _origin_factor(self, origin: str) -> float:
        return {
            "active_choice": 1.0,
            "initial": 0.35,
            "automatic_generation": 0.25,
            "summon_or_periodic": 0.20,
            "conversion": 0.30,
            "one_time_temporary": 0.08,
            "removal_effect": 0.25,
            # Missing provenance is treated conservatively.  It can come
            # from a hand-built test state or an older telemetry snapshot;
            # under-counting its occupancy would make deletion too timid.
            "unknown": 1.0,
        }.get(origin, 1.0)

    def _generation_profile(self, row: dict[str, Any]) -> float:
        """Estimate entries added by a generator over the remaining horizon.

        This is deliberately an estimate for scoring only.  It never draws
        RNG and does not alter the engine's generation behavior.  The profile
        lets v2 price future pool growth instead of treating every generator
        as equally bad (or equally good).
        """
        horizon = 10.0
        expected = 0.0
        periodic = row.get("periodic_spawn")
        if isinstance(periodic, dict):
            every = max(1.0, float(periodic.get("every", 1)))
            amount = max(1.0, float(periodic.get("amount", 1)))
            expected += horizon / every * amount
        chance = row.get("chance_spawn")
        if isinstance(chance, dict):
            probability = max(0.0, min(1.0, float(chance.get("chance", 0.0))))
            amount = max(1.0, float(chance.get("amount", 1)))
            expected += horizon * probability * amount
        each_spin = row.get("spawn_each_spin")
        if isinstance(each_spin, dict):
            amount = max(1.0, float(each_spin.get("amount", 1)))
            expected += horizon * amount
        if row.get("ingredient_generation") and expected <= 0.0:
            # Scripted component generators declare their ownership in data;
            # use a conservative one-entry baseline when no target cadence is
            # expressible in JSON.
            expected = 1.0
        return min(20.0, expected)

    def _generated_tags(self, engine: GameEngine, row: dict[str, Any]) -> set[str]:
        tags: set[str] = set()
        for field in self._GENERATOR_FIELDS:
            spec = row.get(field)
            if not isinstance(spec, dict):
                continue
            if spec.get("tag"):
                tags.add(str(spec["tag"]))
            if spec.get("id"):
                tags.update(self._tags_for(engine, str(spec["id"])))
        return tags

    def _generator_synergy(self, engine: GameEngine, row: dict[str, Any]) -> float:
        """Score whether generated entries have an observed build sink.

        The score is based only on tags and effect references already present
        in the build.  It intentionally has no card-ID exceptions: generated
        products are favored when they are core tags, feed an active mechanic,
        or match an explicit adjacent/count/aura reference.
        """
        generated = self._generated_tags(engine, row)
        if not generated:
            return 0.0
        state = self.build_state(engine)
        core = set(state["core_tags"])
        primary = set(state["primary_tags"])
        mechanisms = set(state["mechanism_tags"])
        sinks = set(state.get("sink_tags", []))
        score = 0.0
        score += 2.5 * len(generated.intersection(core))
        score += 2.0 * len(generated.intersection(sinks))
        score += 1.25 * len(generated.intersection(mechanisms))
        score += 0.5 * len(generated.intersection(primary - core))
        return min(6.0, score)

    def _candidate_pool_cost(self, engine: GameEngine, def_id: str, origin: str = "active_choice") -> float:
        row = engine.catalog.ingredients[def_id]
        tags = set(row.get("tags", []))
        state = self.build_state(engine)
        fit = len(tags.intersection(set(state["core_tags"]) | set(state["primary_tags"])))
        cost = self._pool_pressure(len(engine.s.ingredients)) * self._origin_factor(origin)
        cost *= self._release_factor(row)
        if fit:
            cost *= max(0.25, 1.0 - 0.2 * fit)
        generation = self._generation_profile(row)
        if generation:
            # Continuous generators have a future occupancy cost.  Existing
            # sinks/core tags rebate that cost, while an unconnected generator
            # is increasingly unattractive once the pool is already large.
            synergy = self._generator_synergy(engine, row)
            synergy_factor = 0.20 if synergy >= 4.0 else 0.45 if synergy >= 2.0 else 1.0
            cost += self._pool_pressure(len(engine.s.ingredients)) * min(5.0, generation * 0.45) * synergy_factor
        value_spec = row.get("value") if isinstance(row.get("value"), dict) else {}
        # Count/aura-style mechanisms need a sufficiently populated pool to
        # pay off.  Keep their occupancy cost lower without naming any card.
        if any(key in value_spec for key in ("count_id", "count_tag", "per", "minimum_count")) or row.get("aura"):
            cost *= 0.65
        if "negative" in tags or "waste" in tags or not row.get("removable", True):
            cost *= 1.35
        return cost

    def _long_term_ingredient_value(self, engine: GameEngine, row: dict[str, Any]) -> float:
        horizon = max(4, min(12, int(engine.s.spins_left or 8)))
        value = 0.0
        periodic = row.get("periodic_gold")
        if periodic:
            if isinstance(periodic, dict):
                value += float(periodic.get("gold", 0)) * horizon / max(1, int(periodic.get("every", 1))) * 1.5
        if row.get("growth_amount"):
            value += float(row["growth_amount"]) * 2.5
        growth_after = row.get("growth_after")
        if growth_after:
            # Older data uses an integer countdown while newer definitions
            # may use {"spins": N, ...}.  Treat both representations alike.
            growth_spins = (
                growth_after.get("spins", horizon)
                if isinstance(growth_after, dict)
                else growth_after
            )
            value += 2.0 * max(0.0, (horizon - float(growth_spins)) / horizon)
        transform_after = row.get("transform_after")
        if transform_after:
            transform_spins = (
                transform_after.get("spins", horizon)
                if isinstance(transform_after, dict)
                else transform_after
            )
            value += 2.5 * max(0.0, (horizon - float(transform_spins)) / horizon)
        if row.get("chance_transform"):
            chance_transform = row["chance_transform"]
            if isinstance(chance_transform, dict):
                value += float(chance_transform.get("chance", 0.0)) * 8.0
        if row.get("chance_spawn"):
            chance_spawn = row["chance_spawn"]
            if isinstance(chance_spawn, dict):
                value += float(chance_spawn.get("chance", 0.0)) * 8.0
        if row.get("periodic_spawn") or row.get("spawn_each_spin"):
            value += 4.0
        if row.get("on_removed"):
            value += 1.5
        if row.get("potion"):
            potion = row["potion"]
            if isinstance(potion, dict):
                value += abs(float(potion.get("gold", 0))) * 0.6
                tokens = potion.get("tokens") or {}
                if isinstance(tokens, dict):
                    value += sum(abs(float(amount)) for amount in tokens.values()) * 3.0
                if potion.get("choice_minimum"):
                    value += 5.0
        if row.get("rarity_multiplier") or row.get("global_modifier"):
            value += 5.0
        return value

    def _archetype_fit(self, engine: GameEngine, row: dict[str, Any]) -> float:
        state = self.build_state(engine)
        tags = set(row.get("tags", []))
        # A shared label alone is weak evidence: a plain base-value filler
        # should not look like a build engine merely because several water or
        # metal cards happen to be present.  Effect-bearing definitions get
        # the stronger archetype weights; raw tag overlap remains a small
        # tie-breaker until a mechanism is actually established.
        effect_keys = {
            key for key, value in row.items()
            if value and key not in {"id", "name", "rarity", "base", "tags", "description", "offerable", "removable"}
        }
        mechanism_tags = set(state["mechanism_tags"])
        mechanism_weight = 1.5 if effect_keys else 0.35
        primary_weight = 0.75 if effect_keys else 0.15
        fit = mechanism_weight * len(tags.intersection(mechanism_tags))
        fit += 0.35 * len(tags.intersection(set(state["core_tags"]) - mechanism_tags))
        fit += primary_weight * len(tags.intersection(state["primary_tags"]) - mechanism_tags)
        generated_tags: set[str] = set()
        for field in self._GENERATOR_FIELDS:
            spec = row.get(field) or {}
            if not isinstance(spec, dict):
                continue
            if spec.get("tag"):
                generated_tags.add(str(spec["tag"]))
            if spec.get("id"):
                generated_tags.update(self._tags_for(engine, str(spec["id"])))
        fit += 1.0 * len(generated_tags.intersection(state["core_tags"]))
        return fit

    def choose(self, engine: GameEngine, choice: PendingChoice) -> int | None:
        if not choice.offers:
            return None
        scored = [
            (self.score(engine, choice.kind, def_id), def_id, index)
            for index, def_id in enumerate(choice.offers, 1)
        ]
        # Definition ids make ties stable without consuming game RNG.
        _, selected_id, selected_index = max(scored, key=lambda row: (row[0], row[1]))
        if choice.kind == "ingredient" and choice.can_skip:
            selected_cost = self._candidate_pool_cost(engine, selected_id)
            selected_fit = self._archetype_fit(engine, engine.catalog.ingredients[selected_id])
            selected_components = self.score_components(engine, "ingredient", selected_id)
            selected_future_value = (
                selected_components.get("long_term", 0.0)
                + selected_components.get("immediate", 0.0)
            )
            # A large pool is tolerated when the candidate is a core or
            # generator piece. Only low-value, high-cost active choices are
            # skipped; naturally generated entries are handled by provenance.
            if (
                len(engine.s.ingredients) >= 30
                and selected_cost > 1.0
                and (selected_fit < 3.5 or selected_future_value < 8.0)
            ):
                return None
            if (
                len(engine.s.ingredients) >= 25
                and selected_cost > 0.75
                and (selected_fit < 2.5 or selected_future_value < 5.0)
            ):
                return None
        if choice.kind == "ingredient" and (
            "waste" in engine.catalog.ingredients[selected_id].get("tags", [])
            or not engine.catalog.ingredients[selected_id].get("removable", True)
        ) and choice.can_skip:
            return None
        return selected_index

    def should_reroll(self, engine: GameEngine, choice: PendingChoice) -> bool:
        if choice.kind == "essence" or engine.s.tokens.get("roll", 0) <= 0 or not choice.offers:
            return False
        scored = [(self.score(engine, choice.kind, def_id), def_id) for def_id in choice.offers]
        best, best_id = max(scored, key=lambda row: (row[0], row[1]))
        # The threshold adapts to pool cost and current build fit. A high
        # rarity but off-build candidate is not automatically protected.
        # Keep a modest floor for low-information ingredient offers.  The
        # build-fit rebate below still protects a genuinely synergistic card,
        # while a plain low-value offer remains worth rerolling when a token
        # is available.
        threshold = 6.5 if choice.kind == "ingredient" else 8.0
        if choice.kind == "ingredient":
            threshold += min(3.0, self._candidate_pool_cost(engine, best_id) * 0.5)
            threshold -= min(2.0, self._archetype_fit(engine, engine.catalog.ingredients[best_id]))
        return best < threshold

    def removal_index(self, engine: GameEngine) -> int | None:
        pool_size = len(engine.s.ingredients)
        if engine.s.tokens.get("remove", 0) <= 0 or pool_size <= 25:
            return None
        candidates = []
        for index, instance in enumerate(engine.s.ingredients, 1):
            row = engine.catalog.ingredients[instance.def_id]
            if not row.get("removable", True):
                continue
            components = self.score_components(engine, "ingredient", instance.def_id)
            origin = str(instance.flags.get("_sim_origin", "unknown"))
            cost = self._candidate_pool_cost(engine, instance.def_id, origin)
            fit = self._archetype_fit(engine, row)
            retention = sum(components.values()) + fit
            # Delete priority is a joint value/cost decision. It prefers
            # actively selected, low-value clutter and protects core-generated
            # pieces even when the pool is naturally large.
            deletion_score = retention - cost * 2.5
            if fit >= 2.0 and self._long_term_ingredient_value(engine, row) >= 4.0:
                deletion_score += 4.0
            candidates.append((deletion_score, cost, fit, index))
        if not candidates:
            return None
        weakest_score, weakest_cost, weakest_fit, weakest_index = min(candidates, key=lambda row: (row[0], row[1], row[3]))
        # At 26-29 remove only clear clutter; at 30+ remove high-cost clutter
        # or a genuinely low-retention entry. No fixed card IDs are used.
        if pool_size >= 30 and (weakest_cost >= 1.0 or weakest_score < 7.0) and weakest_fit < 2.0:
            return weakest_index
        if pool_size >= 26 and weakest_cost >= 1.8 and weakest_score < 6.0 and weakest_fit < 1.0:
            return weakest_index
        return None

    def score_components(self, engine: GameEngine, kind: str, def_id: str) -> dict[str, float]:
        if kind == "ingredient":
            row = engine.catalog.ingredients[def_id]
            rarity = int(row.get("rarity", 1))
            base = float(row.get("base", row.get("value", 0)))
            tags = set(row.get("tags", []))
            components = {
                # Rarity is a tie-breaker, not the main value of a complex
                # ingredient. Effects and build fit carry the decision.
                "rarity": rarity * 2.5,
                "immediate": base * 1.5,
                "long_term": self._long_term_ingredient_value(engine, row),
                "synergy": self._archetype_fit(engine, row),
                "risk": 0.0,
                "pool_pressure": 0.0,
                "pool_cost": 0.0,
                "source_fit": 0.0,
            }
            if "negative" in tags:
                components["risk"] -= 12.0
            if "waste" in tags or not row.get("removable", True):
                components["risk"] -= 50.0
            if "equipment" in tags:
                components["long_term"] += 1.5
            existing_tags = {
                tag
                for instance in engine.s.ingredients
                for tag in engine.catalog.ingredients[instance.def_id].get("tags", [])
            }
            if row.get("value", {}).get("if_adjacent_tag") and row["value"]["if_adjacent_tag"] not in existing_tags:
                components["risk"] -= 2.5
            if row.get("value", {}).get("if_adjacent_ids"):
                if not set(row["value"]["if_adjacent_ids"]).intersection(
                    {instance.def_id for instance in engine.s.ingredients}
                ):
                    components["risk"] -= 2.5
            components["synergy"] += 0.75 * len(tags.intersection(existing_tags))
            components["pool_cost"] = -self._candidate_pool_cost(engine, def_id)
            components["pool_pressure"] = components["pool_cost"]
            if any(row.get(field) for field in self._GENERATOR_FIELDS):
                generator_synergy = self._generator_synergy(engine, row)
                components["generation_synergy"] = generator_synergy
            return components

        if kind == "item":
            row = engine.catalog.items[def_id]
            horizon = max(4, min(12, int(engine.s.spins_left or 8)))
            effect_fields = sum(bool(row.get(field)) for field in (
                "bonuses", "periodic_token", "periodic_choice", "periodic_choice_bonus",
                "round_condition", "round_effect", "active", "protect", "on_acquire",
                "rarity_multiplier", "chance_bonus", "spawn_chance_bonus", "script",
            ))
            components = {
                "rarity": int(row.get("rarity", 1)) * 1.5,
                "immediate": float(row.get("per_spin_gold", 0)) * horizon * 1.2,
                "long_term": 0.0,
                "synergy": 0.0,
                "risk": 0.0,
                "pool_pressure": 0.0,
                "pool_cost": 0.0,
                "source_fit": 0.0,
            }
            components["long_term"] += float(row.get("chance_bonus", 0)) * horizon * 2.0
            components["long_term"] += float(row.get("spawn_chance_bonus", 0)) * horizon * 2.0
            components["long_term"] += sum(abs(float(x.get("amount", 0))) for x in row.get("bonuses", [])) * horizon * 0.4
            if row.get("periodic_token"):
                periodic = row["periodic_token"]
                components["long_term"] += float(periodic.get("amount", 1)) * horizon / max(1, int(periodic.get("every", 1))) * 3.0
            if row.get("periodic_choice") or row.get("periodic_choice_bonus"):
                components["long_term"] += 3.0
            if row.get("round_condition") or row.get("round_effect"):
                components["long_term"] += 3.0
            if row.get("active"):
                components["long_term"] += 3.0
            if row.get("rarity_multiplier", 1.0) > 1.0:
                components["long_term"] += (float(row["rarity_multiplier"]) - 1.0) * horizon * 4.0
            components["long_term"] += effect_fields * 0.8
            existing_tags = {
                tag
                for instance in engine.s.ingredients
                for tag in engine.catalog.ingredients[instance.def_id].get("tags", [])
            }
            for bonus in row.get("bonuses", []):
                required = set(bonus.get("tags", [])) | ({bonus["tag"]} if bonus.get("tag") else set())
                if required and not required.intersection(existing_tags):
                    components["risk"] -= 2.0 * len(required)
            if row.get("periodic_gold") or row.get("per_spin_gold"):
                components["long_term"] += horizon * 0.5
            return components

        if kind == "essence":
            row = engine.catalog.essences[def_id]
            effect = row.get("effect", {})
            components = {
                "rarity": float(len(effect)) * 1.5,
                "immediate": float(effect.get("gold", 0)) / 10.0,
                "long_term": 0.0,
                "synergy": 0.0,
                "risk": 0.0,
                "pool_pressure": 0.0,
                "pool_cost": 0.0,
                "source_fit": 0.0,
            }
            components["long_term"] += sum(int(amount) for amount in effect.get("tokens", {}).values()) * 5.0
            if effect.get("permanent_bonus"):
                components["long_term"] += 6.0
            if effect.get("add_ingredient") or effect.get("fixed_choice"):
                components["long_term"] += 4.0
            if effect.get("remove_random_rarity"):
                components["long_term"] += 2.0
            if effect.get("advance_random_essences") or effect.get("copy_recent"):
                components["long_term"] += 3.0
            return components

        raise GameError(f"未知候选类型：{kind}")

    def score(self, engine: GameEngine, kind: str, def_id: str) -> float:
        return sum(self.score_components(engine, kind, def_id).values())


class HeuristicV2Strategy(HeuristicStrategy):
    """A more conservative player model that skips cards and controls the pool.

    v2 deliberately reuses v1's data-derived scoring and archetype detection.
    Its policy change is willingness to accept ingredient choices: once the
    pool is above 20, ordinary low-confidence choices are skipped; at 25 and
    above, deletion is preferred before the next spin whenever a generic
    low-retention entry can be released. Unconnected generators also receive
    a future-growth penalty before selection, while connected/releasable
    generators are protected by generic tag-based exceptions. No definition
    IDs are special-cased and the strategy never consumes RNG of its own.
    """

    name = "heuristic-v2"

    def _generator_penalty(self, engine: GameEngine, row: dict[str, Any]) -> float:
        """Price an unconnected generator before it can win a choice.

        The penalty is generic and derived from expected future additions and
        observed tag sinks.  It is deliberately smaller for high-rarity,
        releasable, or strongly connected generators, so generation is not
        treated as universally weak.
        """
        expected = self._generation_profile(row)
        if expected <= 0.0:
            return 0.0
        synergy = self._generator_synergy(engine, row)
        if synergy < 1.0:
            penalty = min(8.0, 2.0 + expected * 0.75)
        elif synergy < 2.0:
            penalty = min(4.0, expected * 0.35)
        else:
            penalty = 0.0
        pool_size = len(engine.s.ingredients)
        if pool_size >= 20:
            penalty += min(5.0, (pool_size - 19) * 0.15 * max(1.0, expected))
        if self._release_factor(row) < 0.9:
            penalty *= 0.35
        if int(row.get("rarity", 1)) >= 3:
            penalty *= 0.60
        return penalty

    def _v2_score(self, engine: GameEngine, kind: str, def_id: str) -> float:
        score = self.score(engine, kind, def_id)
        if kind == "ingredient":
            score -= self._generator_penalty(engine, engine.catalog.ingredients[def_id])
        return score

    def _is_exception_candidate(self, engine: GameEngine, def_id: str) -> bool:
        """Return whether a candidate deserves protection in a large pool."""
        row = engine.catalog.ingredients[def_id]
        components = self.score_components(engine, "ingredient", def_id)
        fit = self._archetype_fit(engine, row)
        generation_synergy = self._generator_synergy(engine, row)
        immediate_long_term = components.get("immediate", 0.0) + components.get("long_term", 0.0)
        generator = self._generation_profile(row) > 0.0
        rarity_exception = int(row.get("rarity", 1)) >= 3 and (
            not generator or generation_synergy >= 1.0 or immediate_long_term >= 8.0
        )
        return (
            rarity_exception
            or fit >= 2.5
            or generation_synergy >= 2.0
            or self._release_factor(row) < 0.9
            or immediate_long_term >= 8.0
        )

    def _acceptance_threshold(self, engine: GameEngine, def_id: str) -> float:
        pool_size = len(engine.s.ingredients)
        if pool_size <= 20:
            return float("-inf")
        if pool_size < 25:
            threshold = 8.0 + (pool_size - 20) * 0.5
        else:
            threshold = 10.5 + min(5.0, (pool_size - 25) * 0.7)
        fit = self._archetype_fit(engine, engine.catalog.ingredients[def_id])
        generation_synergy = self._generator_synergy(engine, engine.catalog.ingredients[def_id])
        return threshold - min(3.5, fit * 0.7 + generation_synergy * 0.4)

    def choose(self, engine: GameEngine, choice: PendingChoice) -> int | None:
        if not choice.offers:
            return None
        scored = [
            (self._v2_score(engine, choice.kind, def_id), def_id, index)
            for index, def_id in enumerate(choice.offers, 1)
        ]
        _, selected_id, selected = max(scored, key=lambda row: (row[0], row[1]))
        if choice.kind != "ingredient" or not choice.can_skip:
            return selected
        row = engine.catalog.ingredients[selected_id]
        if ("waste" in row.get("tags", []) or not row.get("removable", True)):
            return None
        score = self._v2_score(engine, "ingredient", selected_id)
        future = self._long_term_ingredient_value(
            engine, row
        )
        fit = self._archetype_fit(engine, row)
        pool_size = len(engine.s.ingredients)
        exception = self._is_exception_candidate(engine, selected_id)
        if (
            pool_size >= 15
            and self._generation_profile(row) > 0.0
            and self._generator_penalty(engine, row) > 0.0
            and not exception
            and future < 8.0
        ):
            return None
        if pool_size > 20 and score < self._acceptance_threshold(engine, selected_id) and not exception:
            return None
        # At 20-24, ordinary level-1 filler should usually be skipped unless
        # it directly advances an observed build or pays off immediately.
        if 20 < pool_size < 25 and int(row.get("rarity", 1)) <= 1 and not exception:
            return None
        if pool_size >= 25 and future < 4.0 and fit < 3.0 and not exception:
            return None
        return selected

    def should_reroll(self, engine: GameEngine, choice: PendingChoice) -> bool:
        if choice.kind != "ingredient" or not choice.offers:
            return super().should_reroll(engine, choice)
        if engine.s.tokens.get("roll", 0) <= 0:
            return False
        scored = [
            (self._v2_score(engine, "ingredient", def_id), def_id)
            for def_id in choice.offers
        ]
        best, best_id = max(scored, key=lambda row: (row[0], row[1]))
        if len(engine.s.ingredients) <= 20:
            return super().should_reroll(engine, choice)
        fit = self._archetype_fit(engine, engine.catalog.ingredients[best_id])
        threshold = self._acceptance_threshold(engine, best_id)
        return best < threshold and fit < 3.5 and not self._is_exception_candidate(engine, best_id)

    def removal_index(self, engine: GameEngine) -> int | None:
        pool_size = len(engine.s.ingredients)
        if engine.s.tokens.get("remove", 0) <= 0 or pool_size < 25:
            return None
        candidates = []
        for index, instance in enumerate(engine.s.ingredients, 1):
            row = engine.catalog.ingredients[instance.def_id]
            if not row.get("removable", True):
                continue
            components = self.score_components(engine, "ingredient", instance.def_id)
            origin = str(instance.flags.get("_sim_origin", "unknown"))
            cost = self._candidate_pool_cost(engine, instance.def_id, origin)
            fit = self._archetype_fit(engine, row)
            retention = self._v2_score(engine, "ingredient", instance.def_id) + fit
            deletion_score = retention - cost * 2.5
            if fit >= 2.0 and self._long_term_ingredient_value(engine, row) >= 4.0:
                deletion_score += 4.0
            candidates.append((deletion_score, cost, fit, index))
        if not candidates:
            return None
        weakest_score, weakest_cost, weakest_fit, weakest_index = min(
            candidates, key=lambda row: (row[0], row[1], row[3])
        )
        weakest_id = engine.s.ingredients[weakest_index - 1].def_id
        if self._is_exception_candidate(engine, weakest_id):
            return None
        if pool_size >= 30 and (weakest_cost >= 0.55 or weakest_score < 9.5):
            return weakest_index
        if pool_size >= 25 and (weakest_cost >= 1.0 or weakest_score < 8.0):
            return weakest_index
        return None


class HeuristicV3Strategy(HeuristicV2Strategy):
    """A stricter pool-control policy layered on top of heuristic-v2.

    v3 is intentionally a separate class so v2 remains an unchanged A/B
    baseline.  It uses the same deterministic score and RNG behavior, then
    adds generic pool-band gates and a data-driven check for whether a
    generator's products have a reliable sink (self-consumption, removal,
    transformation, or an observed tag/count mechanism).
    """

    name = "heuristic-v3"

    def _generator_target_rows(
        self, engine: GameEngine, row: dict[str, Any]
    ) -> list[dict[str, Any]]:
        targets: list[dict[str, Any]] = []
        seen: set[str] = set()
        for field in self._GENERATOR_FIELDS:
            spec = row.get(field)
            if not isinstance(spec, dict):
                continue
            target_id = spec.get("id")
            if target_id and str(target_id) in engine.catalog.ingredients:
                target = engine.catalog.ingredients[str(target_id)]
                if str(target_id) not in seen:
                    targets.append(target)
                    seen.add(str(target_id))
            target_tag = spec.get("tag")
            target_rarity = spec.get("rarity")
            for target in engine.catalog.ingredients.values():
                if target["id"] in seen:
                    continue
                if target_tag and target_tag not in target.get("tags", []):
                    continue
                if target_rarity and int(target.get("rarity", 0)) != int(target_rarity):
                    continue
                if target_tag or target_rarity:
                    targets.append(target)
                    seen.add(target["id"])
        return targets

    def _generator_processing_strength(
        self, engine: GameEngine, row: dict[str, Any]
    ) -> float:
        """Estimate whether generated products have a reusable sink.

        All signals come from generic definition fields and currently owned
        definitions.  The method deliberately does not mention a particular
        ingredient or item ID, so newly added content can participate without
        strategy edits.
        """
        targets = self._generator_target_rows(engine, row)
        if not targets:
            return 0.0
        target_ids = {str(target["id"]) for target in targets}
        target_tags = {
            tag for target in targets for tag in target.get("tags", [])
        }
        strength = 0.0

        # Products which disappear, transform, or self-trigger are not
        # permanent pool pollution.
        for target in targets:
            if target.get("potion"):
                strength += 3.0
            if target.get("remove_after") or target.get("transform_after"):
                strength += 2.0
            if target.get("chance_transform"):
                strength += 1.5

        # Existing ingredient mechanics that explicitly consume/count or
        # transform the generated family provide a stable build sink.
        for instance in engine.s.ingredients:
            current = engine.catalog.ingredients.get(instance.def_id, {})
            value = current.get("value")
            if isinstance(value, dict):
                if value.get("count_tag") in target_tags:
                    strength += 2.5
                if value.get("if_adjacent_tag") in target_tags:
                    strength += 2.0
                if target_ids.intersection(str(x) for x in value.get("if_adjacent_ids", []) or []):
                    strength += 2.5
                if target_ids.intersection(str(x) for x in value.get("count_ids", []) or []):
                    strength += 2.5
            for transform_field in ("transform_after", "chance_transform"):
                transform = current.get(transform_field)
                if isinstance(transform, dict) and str(transform.get("into")) in target_ids:
                    strength += 2.0
            aura = current.get("aura")
            if isinstance(aura, dict) and (
                aura.get("tag") in target_tags
                or aura.get("target_tag") in target_tags
                or aura.get("count_tag") in target_tags
            ):
                strength += 1.5

        # Declarative item effects can remove or directly improve the target
        # family.  A chance-based remover is weighted by its declared chance.
        for item_id in engine.s.items:
            item = engine.catalog.items.get(item_id, {})
            for bonus in item.get("bonuses", []) or []:
                if not isinstance(bonus, dict):
                    continue
                bonus_tags = set(bonus.get("tags", []))
                if bonus.get("tag"):
                    bonus_tags.add(str(bonus["tag"]))
                if bonus_tags.intersection(target_tags) or target_ids.intersection(
                    str(x) for x in bonus.get("ids", []) or []
                ):
                    strength += 1.5
            remove_rule = (item.get("round_effect") or {}).get("remove_tag_chance")
            if isinstance(remove_rule, dict) and remove_rule.get("tag") in target_tags:
                strength += min(3.0, max(0.0, float(remove_rule.get("chance", 0.0)) * 6.0))

        # Delete capacity is a useful but finite fallback; it is deliberately
        # weaker than a stable conversion or consumption loop.
        strength += min(2.0, float(engine.s.tokens.get("remove", 0)) * 0.5)
        return min(10.0, strength)

    def _generator_is_core(self, engine: GameEngine, row: dict[str, Any]) -> bool:
        targets = self._generator_target_rows(engine, row)
        target_tags = {tag for target in targets for tag in target.get("tags", [])}
        state = self.build_state(engine)
        if target_tags.intersection(set(state.get("mechanism_tags", []))):
            return True
        if target_tags.intersection(set(state.get("core_tags", []))):
            return self._archetype_fit(engine, row) >= 2.0 or self._generator_synergy(engine, row) >= 2.0
        return False

    def _economic_pressure(self, engine: GameEngine) -> float:
        amount, _ = engine.current_order()
        gap = max(0, int(amount) - int(engine.s.gold))
        if amount <= 0:
            return 0.0
        return min(3.0, gap / float(amount) * 3.0)

    def _v3_exception(self, engine: GameEngine, def_id: str) -> bool:
        row = engine.catalog.ingredients[def_id]
        components = self.score_components(engine, "ingredient", def_id)
        fit = self._archetype_fit(engine, row)
        future = self._long_term_ingredient_value(engine, row)
        immediate = float(components.get("immediate", 0.0))
        generator = self._generation_profile(row) > 0.0
        processing = self._generator_processing_strength(engine, row) if generator else 0.0
        core = self._generator_is_core(engine, row) if generator else fit >= 3.0
        if self._release_factor(row) < 0.9:
            return True
        if core or processing >= 2.0:
            return True
        if int(row.get("rarity", 1)) >= 3 and immediate + future >= 10.0:
            return True
        if immediate + future >= 12.0 and fit >= 1.5:
            return True
        return False

    def _v3_generator_forbidden(self, engine: GameEngine, row: dict[str, Any]) -> bool:
        if self._generation_profile(row) <= 0.0:
            return False
        processing = self._generator_processing_strength(engine, row)
        core = self._generator_is_core(engine, row)
        components = self.score_components(engine, "ingredient", row["id"])
        value = float(components.get("immediate", 0.0)) + float(components.get("long_term", 0.0))
        if core or processing >= 2.0:
            return False
        pool_size = len(engine.s.ingredients)
        if pool_size >= 20:
            return value < 14.0
        return value < 10.0

    def _v2_base_score(self, engine: GameEngine, kind: str, def_id: str) -> float:
        base = HeuristicStrategy.score(self, engine, kind, def_id)
        if kind == "ingredient":
            base -= HeuristicV2Strategy._generator_penalty(
                self, engine, engine.catalog.ingredients[def_id]
            )
        return base

    def _v3_score(self, engine: GameEngine, kind: str, def_id: str) -> float:
        score = self._v2_base_score(engine, kind, def_id)
        if kind != "ingredient":
            return score
        pool_size = len(engine.s.ingredients)
        if pool_size < 15:
            return score
        row = engine.catalog.ingredients[def_id]
        future = self._long_term_ingredient_value(engine, row)
        fit = self._archetype_fit(engine, row)
        exception = self._v3_exception(engine, def_id)
        if self._generation_profile(row) > 0.0:
            processing = self._generator_processing_strength(engine, row)
            if processing < 2.0 and not self._generator_is_core(engine, row):
                score -= min(18.0, 7.0 + self._generation_profile(row) * 1.2)
            elif processing < 4.0:
                score -= 2.0
        if pool_size < 20:
            score -= min(5.0, (pool_size - 14) * 0.8)
            if int(row.get("rarity", 1)) <= 2 and fit < 2.0 and future < 6.0 and not exception:
                score -= 4.0
        else:
            score -= min(9.0, 4.0 + (pool_size - 20) * 0.45)
            if int(row.get("rarity", 1)) <= 2 and fit < 2.5 and future < 8.0 and not exception:
                score -= 6.0
        # Economic urgency can justify a good immediate-income card, but the
        # rebate is bounded so it never erases the pool-dilution cost.
        score += self._economic_pressure(engine) * min(1.5, max(0.0, float(row.get("base", 0))) * 0.2)
        return score

    def score(self, engine: GameEngine, kind: str, def_id: str) -> float:
        return self._v3_score(engine, kind, def_id)

    def _acceptance_threshold_v3(self, engine: GameEngine, def_id: str) -> float:
        pool_size = len(engine.s.ingredients)
        urgency = self._economic_pressure(engine)
        if pool_size < 15:
            return float("-inf")
        if pool_size < 20:
            return 10.0 + (pool_size - 15) * 0.55 - urgency
        return 13.0 + min(8.0, (pool_size - 20) * 0.65) - urgency

    def choose(self, engine: GameEngine, choice: PendingChoice) -> int | None:
        if not choice.offers:
            return None
        # Keeping this exact delegation makes v3 a clean low-pool A/B match
        # for v2, including its tie-breaking and skip behavior.
        if choice.kind == "ingredient" and len(engine.s.ingredients) < 15:
            return super().choose(engine, choice)
        if choice.kind != "ingredient":
            return super().choose(engine, choice)
        scored = [
            (self._v3_score(engine, "ingredient", def_id), def_id, index)
            for index, def_id in enumerate(choice.offers, 1)
        ]
        _, selected_id, selected_index = max(scored, key=lambda row: (row[0], row[1]))
        if not choice.can_skip:
            return selected_index
        row = engine.catalog.ingredients[selected_id]
        if "waste" in row.get("tags", []) or not row.get("removable", True):
            return None
        if self._v3_generator_forbidden(engine, row):
            return None
        score = self._v3_score(engine, "ingredient", selected_id)
        exception = self._v3_exception(engine, selected_id)
        if not exception and score < self._acceptance_threshold_v3(engine, selected_id):
            return None
        if len(engine.s.ingredients) >= 20 and not exception:
            fit = self._archetype_fit(engine, row)
            future = self._long_term_ingredient_value(engine, row)
            if int(row.get("rarity", 1)) <= 2 and fit < 2.5 and future < 8.0:
                return None
        return selected_index

    def should_reroll(self, engine: GameEngine, choice: PendingChoice) -> bool:
        if choice.kind == "ingredient" and len(engine.s.ingredients) >= 15:
            if engine.s.tokens.get("roll", 0) <= 0 or not choice.offers:
                return False
            best_id = max(
                choice.offers,
                key=lambda def_id: (self._v3_score(engine, "ingredient", def_id), def_id),
            )
            row = engine.catalog.ingredients[best_id]
            return self._v3_generator_forbidden(engine, row) or (
                not self._v3_exception(engine, best_id)
                and self._v3_score(engine, "ingredient", best_id)
                < self._acceptance_threshold_v3(engine, best_id)
            )
        return super().should_reroll(engine, choice)

    def removal_index(self, engine: GameEngine) -> int | None:
        pool_size = len(engine.s.ingredients)
        if engine.s.tokens.get("remove", 0) <= 0 or pool_size < 20:
            return None
        candidates = []
        for index, instance in enumerate(engine.s.ingredients, 1):
            row = engine.catalog.ingredients[instance.def_id]
            if not row.get("removable", True):
                continue
            score = self._v3_score(engine, "ingredient", instance.def_id)
            fit = self._archetype_fit(engine, row)
            cost = self._candidate_pool_cost(
                engine,
                instance.def_id,
                str(instance.flags.get("_sim_origin", "unknown")),
            )
            core = fit >= 3.0 or self._generator_is_core(engine, row)
            if core:
                continue
            candidates.append((score - cost * 2.5, cost, fit, index))
        if not candidates:
            return None
        weakest_score, weakest_cost, weakest_fit, weakest_index = min(
            candidates, key=lambda item: (item[0], item[1], item[3])
        )
        if pool_size >= 25 and (weakest_score < 8.0 or weakest_cost >= 0.8):
            return weakest_index
        if pool_size >= 20 and (weakest_score < 5.0 or weakest_cost >= 1.5):
            return weakest_index
        return None


class HeuristicV31Strategy(HeuristicV3Strategy):
    """A graduated variant of v3 with softer early pool control.

    v3.1 is deliberately separate from v3 for later A/B comparisons.  It
    follows the v2 decision path below 18 ingredients, then increases pool
    pressure in three bands.  Generator entries are never categorically
    forbidden: their score receives a generic penalty based on generation
    mode, expected future additions, and the current build's processing
    signals.  Continuous/recursive generators are priced above periodic
    generators, which are priced above one-time generators.
    """

    name = "heuristic-v3.1"
    _ONE_TIME_GENERATOR_FIELDS = (
        "one_time_spawn",
        "spawn_once",
        "on_acquire_spawn",
        "on_obtain_spawn",
    )

    def __init__(self) -> None:
        super().__init__()
        # A dedicated baseline keeps the <18 path exactly aligned with v2
        # without calling v2's _v2_score through this class's score override.
        self._v2_baseline = HeuristicV2Strategy()

    def _generator_class_and_weight(
        self, engine: GameEngine, row: dict[str, Any]
    ) -> tuple[str | None, float, float]:
        """Return (class, penalty weight, expected additions).

        Existing engine fields are treated as recurring when they are checked
        each spin.  The one-time names are accepted for future data-driven
        definitions without making a card-ID exception here.
        """
        targets = self._generator_target_rows(engine, row)
        recursive = any(
            any(target.get(field) for field in self._GENERATOR_FIELDS)
            for target in targets
        )
        if row.get("spawn_each_spin") or row.get("chance_spawn"):
            expected = self._generation_profile(row)
            return ("recursive" if recursive else "continuous", 3.6 if recursive else 2.7, expected)
        if row.get("periodic_spawn"):
            expected = self._generation_profile(row)
            return ("recursive" if recursive else "periodic", 2.8 if recursive else 2.4, expected)
        if row.get("ingredient_generation"):
            return ("continuous", 2.7, self._generation_profile(row))
        for field in self._ONE_TIME_GENERATOR_FIELDS:
            spec = row.get(field)
            if spec:
                amount = float(spec.get("amount", 1)) if isinstance(spec, dict) else 1.0
                return "one_time", 0.85, max(1.0, amount)
        return None, 0.0, 0.0

    def _generator_penalty_v31(self, engine: GameEngine, row: dict[str, Any]) -> float:
        kind, weight, expected = self._generator_class_and_weight(engine, row)
        if not kind or expected <= 0.0:
            return 0.0
        pool_size = len(engine.s.ingredients)
        if pool_size < 18:
            return 0.0
        if pool_size < 20:
            band_factor = 0.65
        elif pool_size < 25:
            band_factor = 1.15
        else:
            band_factor = 1.75 + min(0.75, (pool_size - 25) * 0.08)
        penalty = min(22.0, expected * weight * band_factor)

        # A generator can be a valid build starting point.  Processing and
        # core signals reduce, but never erase, the occupancy penalty.
        processing = self._generator_processing_strength(engine, row)
        if self._generator_is_core(engine, row):
            penalty *= 0.35
        elif processing >= 4.0:
            penalty *= 0.50
        elif processing >= 2.0:
            penalty *= 0.70
        if self._release_factor(row) < 0.9:
            penalty *= 0.55
        return penalty

    def _v31_score(self, engine: GameEngine, kind: str, def_id: str) -> float:
        score = self._v2_base_score(engine, kind, def_id)
        if kind != "ingredient":
            return score
        pool_size = len(engine.s.ingredients)
        if pool_size < 18:
            return score
        row = engine.catalog.ingredients[def_id]
        future = self._long_term_ingredient_value(engine, row)
        fit = self._archetype_fit(engine, row)
        exception = self._v3_exception(engine, def_id)
        score -= self._generator_penalty_v31(engine, row)
        if pool_size < 20:
            score -= min(2.0, (pool_size - 17) * 0.75)
            if int(row.get("rarity", 1)) <= 2 and fit < 2.0 and future < 6.0 and not exception:
                score -= 1.5
        elif pool_size < 25:
            score -= min(7.0, 2.5 + (pool_size - 20) * 0.8)
            if int(row.get("rarity", 1)) <= 2 and fit < 2.5 and future < 8.0 and not exception:
                score -= 3.5
        else:
            score -= min(13.0, 5.0 + (pool_size - 25) * 0.55)
            if int(row.get("rarity", 1)) <= 2 and fit < 3.0 and future < 9.0 and not exception:
                score -= 6.0
        score += self._economic_pressure(engine) * min(
            1.5, max(0.0, float(row.get("base", 0))) * 0.2
        )
        return score

    def score(self, engine: GameEngine, kind: str, def_id: str) -> float:
        return self._v31_score(engine, kind, def_id)

    def _acceptance_threshold_v31(self, engine: GameEngine) -> float:
        pool_size = len(engine.s.ingredients)
        urgency = self._economic_pressure(engine)
        if pool_size < 18:
            return float("-inf")
        if pool_size < 20:
            return 6.0 + (pool_size - 18) * 0.45 - urgency
        if pool_size < 25:
            return 10.0 + (pool_size - 20) * 0.65 - urgency
        return 14.0 + min(10.0, (pool_size - 25) * 0.70) - urgency

    def _choose_v2_baseline(self, engine: GameEngine, choice: PendingChoice) -> int | None:
        return self._v2_baseline.choose(engine, choice)

    def choose(self, engine: GameEngine, choice: PendingChoice) -> int | None:
        if not choice.offers:
            return None
        if choice.kind != "ingredient" or len(engine.s.ingredients) < 18:
            return self._choose_v2_baseline(engine, choice)
        scored = [
            (self._v31_score(engine, "ingredient", def_id), def_id, index)
            for index, def_id in enumerate(choice.offers, 1)
        ]
        _, selected_id, selected_index = max(scored, key=lambda row: (row[0], row[1]))
        if not choice.can_skip:
            return selected_index
        row = engine.catalog.ingredients[selected_id]
        if "waste" in row.get("tags", []) or not row.get("removable", True):
            return None
        exception = self._v3_exception(engine, selected_id)
        if not exception and self._v31_score(engine, "ingredient", selected_id) < self._acceptance_threshold_v31(engine):
            return None
        if len(engine.s.ingredients) >= 20 and not exception:
            fit = self._archetype_fit(engine, row)
            future = self._long_term_ingredient_value(engine, row)
            if int(row.get("rarity", 1)) <= 2 and fit < 2.5 and future < 8.0:
                return None
        if len(engine.s.ingredients) >= 25 and not exception:
            if self._generator_class_and_weight(engine, row)[0] and self._generator_penalty_v31(engine, row) >= 12.0:
                return None
        return selected_index

    def should_reroll(self, engine: GameEngine, choice: PendingChoice) -> bool:
        if choice.kind == "ingredient" and len(engine.s.ingredients) >= 18:
            if engine.s.tokens.get("roll", 0) <= 0 or not choice.offers:
                return False
            best_id = max(
                choice.offers,
                key=lambda def_id: (self._v31_score(engine, "ingredient", def_id), def_id),
            )
            return (
                not self._v3_exception(engine, best_id)
                and self._v31_score(engine, "ingredient", best_id) < self._acceptance_threshold_v31(engine)
            )
        return self._v2_baseline.should_reroll(engine, choice)

    def removal_index(self, engine: GameEngine) -> int | None:
        pool_size = len(engine.s.ingredients)
        if engine.s.tokens.get("remove", 0) <= 0 or pool_size < 20:
            return None
        candidates = []
        for index, instance in enumerate(engine.s.ingredients, 1):
            row = engine.catalog.ingredients[instance.def_id]
            if not row.get("removable", True):
                continue
            fit = self._archetype_fit(engine, row)
            if fit >= 3.0 or self._generator_is_core(engine, row):
                continue
            cost = self._candidate_pool_cost(
                engine,
                instance.def_id,
                str(instance.flags.get("_sim_origin", "unknown")),
            )
            retention = self._v31_score(engine, "ingredient", instance.def_id) + fit
            candidates.append((retention - cost * 2.5, cost, fit, index))
        if not candidates:
            return None
        weakest_score, weakest_cost, weakest_fit, weakest_index = min(
            candidates, key=lambda item: (item[0], item[1], item[3])
        )
        if pool_size >= 25 and (weakest_score < 8.0 or weakest_cost >= 0.8):
            return weakest_index
        if pool_size >= 20 and (weakest_score < 5.0 or weakest_cost >= 1.5):
            return weakest_index
        return None


def strategy_from_name(name: str) -> SimulationStrategy:
    """Construct one of the built-in deterministic simulation strategies."""
    strategies = {
        "heuristic-v1": HeuristicStrategy,
        "heuristic-v2": HeuristicV2Strategy,
        "heuristic-v3": HeuristicV3Strategy,
        "heuristic-v3.1": HeuristicV31Strategy,
    }
    try:
        return strategies[name]()
    except KeyError as exc:
        raise ValueError(f"未知模拟策略：{name}") from exc


@dataclass
class GameRecord:
    index: int
    seed: int
    status: str
    won: bool
    end_layer: int
    orders_completed: int
    spins: int
    action_count: int
    gold: int
    final_attributes: dict[str, Any]
    held_items: list[str]
    held_ingredients: list[str]
    held_equipment: list[str]
    held_essences: list[str]
    content_stats: dict[str, dict[str, dict[str, int]]]
    selected_content: dict[str, list[str]]
    strategy_events: dict[str, Any]
    death_reason: str | None
    error: str | None = None
    endless_orders_completed: int = 0
    highest_endless_order: int = 0
    highest_endless_single_turn_gold: int = 0
    highest_single_turn_gold: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def derive_seed(base_seed: int, game_index: int) -> int:
    """Derive independent, repeatable per-game seeds without touching game RNG."""
    value = (base_seed & MASK64) + (game_index * 0x9E3779B97F4A7C15)
    value ^= value >> 30
    value = (value * 0xBF58476D1CE4E5B9) & MASK64
    value ^= value >> 27
    value = (value * 0x94D049BB133111EB) & MASK64
    return (value ^ (value >> 31)) & MASK64


def validate_simulation_state(engine: GameEngine) -> list[str]:
    """Return invariant violations that should abort a simulation safely."""
    state = engine.s
    errors: list[str] = []
    if state.gold < 0:
        errors.append("gold<0")
    if not 1 <= state.difficulty <= 15:
        errors.append("difficulty_out_of_range")
    if state.fun_mode not in {"none", "giant", "rapid", "blind_box", "minimal", "mutation"}:
        errors.append("fun_mode_invalid")
    if state.order_index < 0 or (not state.endless_mode and state.order_index > 13):
        errors.append("order_index_out_of_range")
    if state.spins_left < 0:
        errors.append("spins_left<0")
    if any(int(value) < 0 for value in state.tokens.values()):
        errors.append("token<0")
    uids = [instance.uid for instance in state.ingredients]
    if len(uids) != len(set(uids)):
        errors.append("duplicate_ingredient_uid")
    for choice in state.pending:
        # Bundle offers are definitions embedded in the pending choice, not
        # run-end option IDs.  Resolve this branch before the fallback that
        # calls _definition_view("run_end", ...); otherwise a bundle such as
        # pigment_box's ``accept_pigments`` is incorrectly rejected as an
        # unknown run-end option during simulation validation.
        if choice.kind == "ingredient":
            collection = engine.catalog.ingredients
        elif choice.kind == "item":
            collection = engine.catalog.items
        elif choice.kind == "essence":
            collection = engine.catalog.essences
        elif choice.kind == "bundle":
            collection = choice.details.get("options", {})
        else:
            collection = {
                def_id: engine._definition_view("run_end", def_id)
                for def_id in choice.offers
            }
        if any(def_id not in collection for def_id in choice.offers):
            errors.append("pending_offer_missing_definition")
    return errors


def _failure_reason(state: GameState, status: str, error: str | None) -> str | None:
    if status == "won":
        return None
    if status == "aborted":
        return error or "simulation_aborted"
    if status == "lost":
        if any("订单失败" in line for line in state.last_log):
            return "order_timeout"
        return "unknown_game_loss"
    return None


def _content_category(catalog: Catalog, kind: str, def_id: str) -> str | None:
    if kind == "item":
        return "items"
    if kind == "essence":
        return "essences"
    if kind == "ingredient":
        if "equipment" in catalog.ingredients[def_id].get("tags", []):
            return "equipment"
        return "ingredients"
    return None


def _game_content_row() -> dict[str, int]:
    return {
        "offer_count": 0,
        "choice_count": 0,
        "acquisition_count": 0,
        "final_owned_count": 0,
        "trigger_count": 0,
        "consumed_count": 0,
    }


def _final_attributes(engine: GameEngine, max_pool_size: int | None = None) -> dict[str, Any]:
    state = engine.s
    return {
        "status": state.status,
        "difficulty": state.difficulty,
        "spin": state.spin,
        "order_index": state.order_index,
        "spins_left": state.spins_left,
        "pool_size": len(state.ingredients),
        "max_pool_size": max_pool_size if max_pool_size is not None else len(state.ingredients),
        "board_capacity": engine.board_capacity(),
        "fun_mode": state.fun_mode,
        "tokens": dict(state.tokens),
        "rarity_multiplier": engine.current_rarity_multiplier(),
        "expanded": state.expanded,
        "event_counts": dict(state.stats.get("event_counts", {})),
        "endless_mode": bool(state.endless_mode),
        "endless_order": int(state.endless_order),
        "endless_target": int(state.endless_target),
        "peace_mode": bool(state.peace_mode),
        "peace_order": int(state.peace_order),
        "endless_orders_completed": int(state.stats.get("endless_orders_completed", 0)),
        "highest_endless_order": int(state.stats.get("highest_endless_order", 0)),
        "highest_endless_single_turn_gold": int(state.stats.get("highest_endless_single_turn_gold", 0)),
        "highest_single_turn_gold": int(state.stats.get("highest_single_turn_gold", 0)),
    }


def simulate_game(
    seed: int,
    difficulty: int = 1,
    *,
    strategy: SimulationStrategy | None = None,
    game_index: int = 0,
    max_actions: int = 5000,
    on_choice: Callable[[GameEngine, PendingChoice, str | None], None] | None = None,
    on_spin: Callable[[GameEngine], None] | None = None,
    on_start: Callable[[GameEngine], None] | None = None,
    catalog: Catalog | None = None,
    fun_mode: str = "none",
) -> GameRecord:
    """Run one complete game through the real engine and return its telemetry."""
    policy = strategy or HeuristicStrategy()
    engine = GameEngine(catalog)
    engine.new_game(seed, difficulty, fun_mode)
    if on_start:
        on_start(engine)
    selected: dict[str, set[str]] = {
        "items": set(),
        "ingredients": set(),
        "equipment": set(),
        "essences": set(),
    }
    per_game_stats: dict[str, dict[str, dict[str, int]]] = {
        "items": {},
        "ingredients": {},
        "equipment": {},
        "essences": {},
    }
    known_items: Counter[str] = Counter()
    known_essences: Counter[str] = Counter()
    known_ingredient_uids: set[int] = set()
    strategy_events: dict[str, Any] = {
        "rolls": [],
        "deletes": [],
        "choices": [],
        "pool_curve": [],
        "pool_events": [],
        "order_outcomes": [],
        "final_order_curve": [],
    }
    pool_source_counts: Counter[str] = Counter({source: 0 for source in POOL_GROWTH_SOURCES})

    for instance in engine.s.ingredients:
        instance.flags.setdefault("_sim_origin", "initial")

    def build_state() -> dict[str, Any]:
        method = getattr(policy, "build_state", None)
        return method(engine) if method else {"pool_size": len(engine.s.ingredients)}

    def record_pool_delta(
        before: dict[int, str],
        source: str,
        action: str,
        selected_id: str | None = None,
        *,
        growth_source: str | None = None,
        copied_count: int = 0,
    ) -> None:
        current = {instance.uid: instance for instance in engine.s.ingredients}
        generator_ids: set[str] = set()
        generator_tags: set[str] = set()
        for definition in engine.catalog.ingredients.values():
            for field in getattr(policy, "_GENERATOR_FIELDS", ()):
                spec = definition.get(field)
                if not isinstance(spec, dict):
                    continue
                if spec.get("id"):
                    generator_ids.add(str(spec["id"]))
                if spec.get("tag"):
                    generator_tags.add(str(spec["tag"]))

        def matches_generator(def_id: str) -> bool:
            if def_id in generator_ids:
                return True
            tags = set(engine.catalog.ingredients.get(def_id, {}).get("tags", []))
            return bool(tags.intersection(generator_tags))

        new_instances = [
            (uid, instance)
            for uid, instance in current.items()
            if before.get(uid) is None
        ]
        # ``copied`` is an event count rather than a UID-level marker in the
        # engine.  It is sufficient for the requested source totals: reserve
        # that many newly-added instances for the copy bucket, while the
        # remaining additions retain their normal source classification.
        copy_remaining = max(0, min(int(copied_count), len(new_instances)))
        for uid, instance in current.items():
            previous_id = before.get(uid)
            if previous_id is not None and previous_id != instance.def_id:
                instance.flags["_sim_origin"] = "conversion"
                strategy_events["pool_events"].append({
                    "uid": uid, "id": instance.def_id, "source": "conversion", "action": action,
                    "previous_id": previous_id, "pool_size": len(current),
                })
                continue
            if previous_id is not None:
                continue
            if copy_remaining:
                growth_origin = "copy"
                copy_remaining -= 1
            elif growth_source == "item_generation":
                growth_origin = "item_generation"
            elif source == "active_choice" and selected_id == instance.def_id:
                growth_origin = "active_choice"
            elif source == "spin" and instance.def_id == "slag":
                growth_origin = "periodic_slag"
            elif source == "spin":
                growth_origin = "automatic_generation"
            else:
                growth_origin = "other"

            if source == "spin":
                # Only classify a spin-time addition as summon/periodic when
                # its definition matches an actual generator target.  This
                # avoids attributing every generated entry to an unrelated
                # generator merely because one exists elsewhere in the pool.
                origin = "summon_or_periodic" if matches_generator(instance.def_id) else "automatic_generation"
            elif source == "active_choice" and selected_id == instance.def_id:
                origin = "active_choice"
            elif source == "active_choice":
                origin = "conversion"
            elif source == "remove":
                origin = "removal_effect"
            else:
                origin = "one_time_temporary" if "potion" in engine.catalog.ingredients[instance.def_id].get("tags", []) else "conversion"
            instance.flags["_sim_origin"] = origin
            pool_source_counts[growth_origin] += 1
            definition = engine.catalog.ingredients[instance.def_id]
            strategy_events["pool_events"].append({
                "uid": uid, "id": instance.def_id, "source": origin, "action": action,
                "growth_source": growth_origin,
                "rarity": int(definition.get("rarity", 1)), "base": float(definition.get("base", 0)),
                "tags": list(definition.get("tags", [])), "pool_size": len(current),
            })

    def capture_acquisitions(skip_kind: str | None = None, skip_id: str | None = None) -> None:
        nonlocal known_items, known_essences, known_ingredient_uids

        current_items = Counter(engine.s.items)
        item_delta = current_items - known_items
        if skip_kind == "item" and skip_id in item_delta:
            item_delta[skip_id] -= 1
        for def_id, amount in item_delta.items():
            row = per_game_stats["items"].setdefault(
                def_id,
                _game_content_row(),
            )
            row["acquisition_count"] += amount
        removed_items = known_items - current_items
        for def_id, amount in removed_items.items():
            row = per_game_stats["items"].setdefault(def_id, _game_content_row())
            row["consumed_count"] += amount
        known_items = current_items

        current_essences = Counter(engine.s.essences)
        essence_delta = current_essences - known_essences
        if skip_kind == "essence" and skip_id in essence_delta:
            essence_delta[skip_id] -= 1
        for def_id, amount in essence_delta.items():
            row = per_game_stats["essences"].setdefault(
                def_id,
                _game_content_row(),
            )
            row["acquisition_count"] += amount
        known_essences = current_essences

        current_uids = {instance.uid for instance in engine.s.ingredients}
        skipped_ingredient = False
        for instance in engine.s.ingredients:
            if instance.uid in known_ingredient_uids:
                continue
            if (
                not skipped_ingredient
                and skip_kind == "ingredient"
                and instance.def_id == skip_id
            ):
                skipped_ingredient = True
                continue
            category = _content_category(engine.catalog, "ingredient", instance.def_id)
            if category:
                row = per_game_stats[category].setdefault(
                    instance.def_id,
                    _game_content_row(),
                )
                row["acquisition_count"] += 1
        known_ingredient_uids = current_uids

    # The five starting ingredients are already acquired at game creation.
    capture_acquisitions()
    action_count = 0
    max_pool_size = len(engine.s.ingredients)
    roll_streak = 0
    error: str | None = None
    status = "playing"

    while engine.s.status == "playing" and action_count < max_actions:
        try:
            if engine.s.pending:
                choice = engine.s.pending[0]
                if choice.kind == "run_end":
                    # Batch balance runs retain the historical terminal
                    # semantics: choose "end run" instead of entering a
                    # non-terminating infinite branch.
                    engine.choose(1)
                    strategy_events["choices"].append({
                        "kind": choice.kind,
                        "offers": {def_id: {"score": 0.0, "components": {}} for def_id in choice.offers},
                        "selected": "end_run",
                        "pool_size": len(engine.s.ingredients),
                        "build_state": build_state(),
                    })
                    if on_choice:
                        on_choice(engine, choice, "end_run")
                    action_count += 1
                    continue
                if choice.kind == "bundle":
                    # Bundle choices are intentionally all-or-nothing. The
                    # simulator accepts the first declared option so this
                    # special choice remains deterministic and single-step.
                    before_choice_uids = {instance.uid: instance.def_id for instance in engine.s.ingredients}
                    selected_id = choice.offers[0] if choice.offers else None
                    if selected_id is None:
                        raise GameError("组合选择没有可用选项")
                    engine.choose(1)
                    record_pool_delta(
                        before_choice_uids,
                        "active_choice",
                        "choose:bundle",
                        growth_source="item_generation",
                    )
                    capture_acquisitions()
                    strategy_events["choices"].append({
                        "kind": choice.kind,
                        "offers": {option_id: {"score": 0.0, "components": {}} for option_id in choice.offers},
                        "selected": selected_id,
                        "pool_size": len(engine.s.ingredients),
                        "pool_size_before": len(before_choice_uids),
                        "build_state": build_state(),
                    })
                    action_count += 1
                    violations = validate_simulation_state(engine)
                    if violations:
                        error = "state_invariant:" + ",".join(violations)
                        status = "aborted"
                        break
                    continue
                if policy.should_reroll(engine, choice):
                    before_scores = [policy.score(engine, choice.kind, def_id) for def_id in choice.offers]
                    before_max = max(before_scores, default=0.0)
                    before_pool = len(engine.s.ingredients)
                    engine.reroll()
                    rerolled = engine.s.pending[0]
                    after_scores = [policy.score(engine, rerolled.kind, def_id) for def_id in rerolled.offers]
                    after_max = max(after_scores, default=0.0)
                    roll_streak += 1
                    strategy_events["rolls"].append({
                        "kind": choice.kind,
                        "pool_size": before_pool,
                        "before_average": sum(before_scores) / len(before_scores) if before_scores else 0.0,
                        "after_average": sum(after_scores) / len(after_scores) if after_scores else 0.0,
                        "before_max": before_max,
                        "after_max": after_max,
                        "effective": after_max > before_max,
                        "quality_roll": before_max >= (7.0 if choice.kind == "ingredient" else 10.0),
                        "streak": roll_streak,
                    })
                    max_pool_size = max(max_pool_size, len(engine.s.ingredients))
                    action_count += 1
                    violations = validate_simulation_state(engine)
                    if violations:
                        error = "state_invariant:" + ",".join(violations)
                        status = "aborted"
                        break
                    continue
                for def_id in choice.offers:
                    category = _content_category(engine.catalog, choice.kind, def_id)
                    if category:
                        row = per_game_stats[category].setdefault(
                            def_id,
                            _game_content_row(),
                        )
                        row["offer_count"] += 1
                offer_scores = {
                    def_id: {
                        "score": policy.score(engine, choice.kind, def_id),
                        "components": policy.score_components(engine, choice.kind, def_id),
                    }
                    for def_id in choice.offers
                }
                pool_size_before_choice = len(engine.s.ingredients)
                index = policy.choose(engine, choice)
                selected_id: str | None = None
                before_choice_uids = {instance.uid: instance.def_id for instance in engine.s.ingredients}
                if index is None and choice.can_skip:
                    engine.skip()
                else:
                    if index is None:
                        index = 1
                    if not 1 <= index <= len(choice.offers):
                        raise GameError("模拟策略返回了越界候选序号")
                    selected_id = choice.offers[index - 1]
                    engine.choose(index)
                    if choice.kind == "ingredient":
                        # Ingredient choices created by an item (for example
                        # large_reactor) are still actively picked by the
                        # strategy, but their pool growth belongs to the
                        # item-generation bucket. PendingChoice.source is
                        # the shared data-driven provenance field; no item ID
                        # is special-cased here.
                        ingredient_growth_source = (
                            "item_generation"
                            if choice.source in engine.catalog.items
                            else "active_choice"
                        )
                    else:
                        ingredient_growth_source = None
                    record_pool_delta(
                        before_choice_uids,
                        "active_choice" if choice.kind == "ingredient" else "mechanism",
                        f"choose:{choice.kind}",
                        selected_id=selected_id,
                        growth_source=(
                            ingredient_growth_source
                            if ingredient_growth_source is not None
                            else "item_generation"
                            if choice.kind == "item"
                            else "other"
                        ),
                    )
                    category = _content_category(engine.catalog, choice.kind, selected_id)
                    if category:
                        row = per_game_stats[category].setdefault(
                            selected_id,
                            _game_content_row(),
                        )
                        row["choice_count"] += 1
                        # A choice is an acquisition even when an immediate
                        # trigger consumes the item/essence in the same action.
                        row["acquisition_count"] += 1
                        if choice.kind == "item" and selected_id not in engine.s.items:
                            row["consumed_count"] += 1
                capture_acquisitions(choice.kind, selected_id)
                strategy_events["choices"].append({
                    "kind": choice.kind,
                    "offers": offer_scores,
                    "selected": selected_id,
                    "pool_size": len(engine.s.ingredients),
                    "pool_size_before": pool_size_before_choice if choice.kind == "ingredient" else None,
                    "build_state": build_state(),
                })
                roll_streak = 0
                if selected_id:
                    if choice.kind == "item":
                        selected["items"].add(selected_id)
                    elif choice.kind == "essence":
                        selected["essences"].add(selected_id)
                    elif choice.kind == "ingredient":
                        category = _content_category(engine.catalog, "ingredient", selected_id)
                        if category:
                            selected[category].add(selected_id)
                if on_choice:
                    on_choice(engine, choice, selected_id)
            else:
                removal_index = policy.removal_index(engine)
                if removal_index is not None:
                    removed = engine.s.ingredients[removal_index - 1]
                    removed_row = engine.catalog.ingredients[removed.def_id]
                    before_pool = len(engine.s.ingredients)
                    before_remove_uids = {instance.uid: instance.def_id for instance in engine.s.ingredients}
                    removed_score = policy.score(engine, "ingredient", removed.def_id)
                    removed_components = policy.score_components(engine, "ingredient", removed.def_id)
                    engine.remove(removal_index)
                    strategy_events["deletes"].append({
                        "id": removed.def_id,
                        "pool_before": before_pool,
                        "pool_after": len(engine.s.ingredients),
                        "score": removed_score,
                        "components": removed_components,
                        "base_value": float(removed_row.get("base", 0)) + int(removed.permanent_bonus),
                        "negative": "negative" in removed_row.get("tags", []),
                        "tags": list(removed_row.get("tags", [])),
                        "origin": str(removed.flags.get("_sim_origin", "unknown")),
                        "build_state": build_state(),
                    })
                    record_pool_delta(before_remove_uids, "remove", "delete", growth_source="other")
                    capture_acquisitions()
                    max_pool_size = max(max_pool_size, len(engine.s.ingredients))
                    action_count += 1
                    violations = validate_simulation_state(engine)
                    if violations:
                        error = "state_invariant:" + ",".join(violations)
                        status = "aborted"
                        break
                    continue
                order_before_spin = engine.s.order_index
                order_amount_before_spin, _ = engine.current_order()
                gold_before_spin = engine.s.gold
                before_spin_uids = {instance.uid: instance.def_id for instance in engine.s.ingredients}
                copied_before_spin = int(engine.s.stats.get("event_counts", {}).get("copied", 0))
                engine.spin()
                copied_after_spin = int(engine.s.stats.get("event_counts", {}).get("copied", 0))
                record_pool_delta(
                    before_spin_uids,
                    "spin",
                    "spin",
                    copied_count=max(0, copied_after_spin - copied_before_spin),
                )
                capture_acquisitions()
                strategy_events["pool_curve"].append({
                    "spin": engine.s.spin,
                    "pool_size": len(engine.s.ingredients),
                    "gold": engine.s.gold,
                    "income": int(engine.s.stats.get("last_income", 0)),
                    "build_state": build_state(),
                })
                if difficulty >= 10 and order_before_spin >= 12:
                    strategy_events["final_order_curve"].append({
                        "order_index": order_before_spin,
                        "target": int(order_amount_before_spin),
                        "gold_before_spin": int(gold_before_spin),
                        "gold_after_spin": int(engine.s.gold),
                        "spins_left_after": int(engine.s.spins_left),
                        "status_after": engine.s.status,
                    })
                if engine.s.order_index > order_before_spin:
                    for completed_order in range(order_before_spin + 1, engine.s.order_index + 1):
                        strategy_events["order_outcomes"].append({
                            "order": completed_order,
                            "result": "completed",
                            "target": int(order_amount_before_spin),
                            "gold_before": int(gold_before_spin),
                            "gold_after": int(engine.s.gold),
                            "gold_gap": 0,
                        })
                elif engine.s.status == "lost":
                    death_order = order_before_spin + 1
                    strategy_events["order_outcomes"].append({
                        "order": death_order,
                        "result": "died",
                        "target": int(order_amount_before_spin),
                        "gold_before": int(gold_before_spin),
                        "gold_after": int(engine.s.gold),
                        "gold_gap": max(0, int(order_amount_before_spin) - int(engine.s.gold)),
                    })
                max_pool_size = max(max_pool_size, len(engine.s.ingredients))
                if on_spin:
                    on_spin(engine)
            max_pool_size = max(max_pool_size, len(engine.s.ingredients))
            action_count += 1
            violations = validate_simulation_state(engine)
            if violations:
                error = "state_invariant:" + ",".join(violations)
                status = "aborted"
                break
        except (GameError, IndexError, KeyError, TypeError, ValueError) as exc:
            error = f"{type(exc).__name__}:{exc}"
            status = "aborted"
            break

    if status != "aborted":
        if engine.s.status in {"won", "lost"}:
            status = engine.s.status
        else:
            status = "aborted"
            error = f"max_actions_exceeded:{max_actions}"

    state = engine.s
    for def_id, amount in state.stats.get("item_trigger_counts", {}).items():
        if def_id in engine.catalog.items:
            row = per_game_stats["items"].setdefault(def_id, _game_content_row())
            row["trigger_count"] += int(amount)
    for def_id, amount in state.stats.get("essence_hits", {}).items():
        if def_id in engine.catalog.essences:
            row = per_game_stats["essences"].setdefault(def_id, _game_content_row())
            row["trigger_count"] += int(amount)
    for def_id in state.consumed_essences:
        row = per_game_stats["essences"].setdefault(def_id, _game_content_row())
        row["consumed_count"] += 1
    held_items = list(state.items)
    held_ingredients = [
        instance.def_id
        for instance in state.ingredients
        if "equipment" not in engine.catalog.ingredients[instance.def_id].get("tags", [])
    ]
    held_equipment = [
        instance.def_id
        for instance in state.ingredients
        if "equipment" in engine.catalog.ingredients[instance.def_id].get("tags", [])
    ]
    held_essences = list(state.essences)
    strategy_events["final_build_state"] = build_state()
    strategy_events["pool_source_counts"] = dict(pool_source_counts)
    strategy_events["pool_origin_counts"] = dict(Counter(
        str(instance.flags.get("_sim_origin", "unknown")) for instance in state.ingredients
    ))
    held_by_category = {
        "items": Counter(held_items),
        "ingredients": Counter(held_ingredients),
        "equipment": Counter(held_equipment),
        "essences": Counter(held_essences),
    }
    for category, counts in held_by_category.items():
        for def_id, amount in counts.items():
            row = per_game_stats[category].setdefault(
                def_id,
                _game_content_row(),
            )
            row["final_owned_count"] += amount
    end_layer = max(1, state.order_index if status == "won" else state.order_index + 1)
    return GameRecord(
        index=game_index,
        seed=seed,
        status=status,
        won=status == "won",
        end_layer=end_layer,
        orders_completed=state.order_index,
        spins=state.spin,
        action_count=action_count,
        gold=state.gold,
        final_attributes=_final_attributes(engine, max_pool_size),
        held_items=held_items,
        held_ingredients=held_ingredients,
        held_equipment=held_equipment,
        held_essences=held_essences,
        content_stats=per_game_stats,
        selected_content={key: sorted(value) for key, value in selected.items()},
        strategy_events=strategy_events,
        death_reason=_failure_reason(state, status, error),
        error=error,
        endless_orders_completed=int(state.stats.get("endless_orders_completed", 0)),
        highest_endless_order=int(state.stats.get("highest_endless_order", 0)),
        highest_endless_single_turn_gold=int(state.stats.get("highest_endless_single_turn_gold", 0)),
        highest_single_turn_gold=int(state.stats.get("highest_single_turn_gold", 0)),
    )


def _content_row(definition: dict[str, Any], kind: str) -> dict[str, Any]:
    return {
        "id": definition["id"],
        "name": definition.get("name", definition["id"]),
        "kind": kind,
        "rarity": definition.get("rarity"),
        "offer_count": 0,
        "choice_count": 0,
        "acquisition_count": 0,
        "trigger_count": 0,
        "triggered_games": 0,
        "wins_when_triggered": 0,
        "consumed_count": 0,
        "consumed_games": 0,
        "wins_when_consumed": 0,
        "selected_games": 0,
        "final_owned_count": 0,
        "final_owned_games": 0,
        "wins_when_selected": 0,
        "wins_when_owned": 0,
        "selection_rate": 0.0,
        "game_selection_rate": 0.0,
        "possession_rate": 0.0,
        "win_rate_when_selected": None,
        "win_rate_when_owned": None,
        "win_lift_when_selected": None,
        "win_lift_when_owned": None,
        "suspected_balance": None,
    }


class BatchAccumulator:
    def __init__(self, catalog: Catalog, games: int, *, retain_details: bool = True) -> None:
        self.catalog = catalog
        self.games = games
        self.retain_details = retain_details
        self.content: dict[str, dict[str, dict[str, Any]]] = {
            "items": {
                row["id"]: _content_row(row, "item") for row in catalog.items.values()
            },
            "ingredients": {
                row["id"]: _content_row(row, "ingredient")
                for row in catalog.ingredients.values()
                if "equipment" not in row.get("tags", [])
            },
            "equipment": {
                row["id"]: _content_row(row, "equipment")
                for row in catalog.ingredients.values()
                if "equipment" in row.get("tags", [])
            },
            "essences": {
                row["id"]: _content_row(row, "essence") for row in catalog.essences.values()
            },
        }
        self.records: list[GameRecord] = []
        self.max_pool_sizes: list[int] = []
        self.roll_events: list[dict[str, Any]] = []
        self.delete_events: list[dict[str, Any]] = []
        self.pool_origin_counts: Counter[str] = Counter()
        self.pool_event_counts: Counter[str] = Counter()
        self.pool_growth_source_counts: Counter[str] = Counter({source: 0 for source in POOL_GROWTH_SOURCES})
        self.pool_event_sizes: list[float] = []
        self.pool_band_choice_counts: dict[str, Counter[str]] = {
            "under_15": Counter(),
            "15_19": Counter(),
            "20_plus": Counter(),
        }
        self.generator_offer_count = 0
        self.generator_selected_count = 0
        self.order_reached: Counter[int] = Counter()
        self.order_died: Counter[int] = Counter()
        self.order_death_gap_sum: Counter[int] = Counter()
        self.growth: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.total_rolls = 0
        self.total_deletes = 0

    def _category(self, kind: str, def_id: str) -> str | None:
        return _content_category(self.catalog, kind, def_id)

    def observe_choice(self, _engine: GameEngine, choice: PendingChoice, selected_id: str | None) -> None:
        for def_id in choice.offers:
            category = self._category(choice.kind, def_id)
            if category:
                self.content[category][def_id]["offer_count"] += 1
        if selected_id:
            category = self._category(choice.kind, selected_id)
            if category:
                row = self.content[category][selected_id]
                row["choice_count"] += 1

    def observe_spin(self, engine: GameEngine) -> None:
        state = engine.s
        point = self.growth[state.spin]
        point["samples"] += 1
        point["gold_sum"] += state.gold
        point["pool_size_sum"] += len(state.ingredients)
        point["rarity_multiplier_sum"] += engine.current_rarity_multiplier()
        point["items_sum"] += len(state.items)
        point["equipment_sum"] += sum(
            1
            for instance in state.ingredients
            if "equipment" in engine.catalog.ingredients[instance.def_id].get("tags", [])
        )
        point["essences_sum"] += len(state.essences)
        point["roll_tokens_sum"] += int(state.tokens.get("roll", 0))
        point["remove_tokens_sum"] += int(state.tokens.get("remove", 0))
        point["essence_tokens_sum"] += int(state.tokens.get("essence", 0))

    def observe_record(self, record: GameRecord) -> None:
        self.total_rolls += len(record.strategy_events.get("rolls", []))
        self.total_deletes += len(record.strategy_events.get("deletes", []))
        if self.retain_details:
            self.records.append(record)
        else:
            # Keep only fields needed for aggregate rates and curves. Full
            # per-action telemetry is intentionally optional for large scans.
            self.records.append(GameRecord(
                index=record.index,
                seed=record.seed,
                status=record.status,
                won=record.won,
                end_layer=record.end_layer,
                orders_completed=record.orders_completed,
                spins=record.spins,
                action_count=record.action_count,
                gold=record.gold,
                final_attributes=record.final_attributes,
                held_items=[],
                held_ingredients=[],
                held_equipment=[],
                held_essences=[],
                content_stats={},
                selected_content={},
                strategy_events={},
                death_reason=record.death_reason,
                error=record.error,
                endless_orders_completed=record.endless_orders_completed,
                highest_endless_order=record.highest_endless_order,
                highest_endless_single_turn_gold=record.highest_endless_single_turn_gold,
                highest_single_turn_gold=record.highest_single_turn_gold,
            ))
        self.max_pool_sizes.append(int(record.final_attributes.get("max_pool_size", len(record.held_ingredients) + len(record.held_equipment))))
        self.roll_events.extend(record.strategy_events.get("rolls", []))
        self.delete_events.extend(record.strategy_events.get("deletes", []))
        self.pool_origin_counts.update(record.strategy_events.get("pool_origin_counts", {}))
        self.pool_growth_source_counts.update(record.strategy_events.get("pool_source_counts", {}))
        for choice_event in record.strategy_events.get("choices", []):
            if choice_event.get("kind") != "ingredient":
                continue
            pool_size = choice_event.get("pool_size_before")
            if pool_size is None:
                continue
            pool_size = int(pool_size)
            band = "under_15" if pool_size < 15 else "15_19" if pool_size < 20 else "20_plus"
            stats = self.pool_band_choice_counts[band]
            stats["choices"] += 1
            if choice_event.get("selected") is None:
                stats["skipped"] += 1
            else:
                stats["selected"] += 1
            for def_id in choice_event.get("offers", {}):
                definition = self.catalog.ingredients.get(def_id, {})
                if any(definition.get(field) for field in HeuristicStrategy._GENERATOR_FIELDS):
                    self.generator_offer_count += 1
            selected_id = choice_event.get("selected")
            if selected_id and selected_id in self.catalog.ingredients:
                definition = self.catalog.ingredients[selected_id]
                if any(definition.get(field) for field in HeuristicStrategy._GENERATOR_FIELDS):
                    self.generator_selected_count += 1
        for outcome in record.strategy_events.get("order_outcomes", []):
            order = int(outcome.get("order", 0))
            if order <= 0:
                continue
            self.order_reached[order] += 1
            if outcome.get("result") == "died":
                self.order_died[order] += 1
                self.order_death_gap_sum[order] += max(0, int(outcome.get("gold_gap", 0)))
        for event in record.strategy_events.get("pool_events", []):
            self.pool_event_counts[str(event.get("source", "unknown"))] += 1
            if event.get("pool_size") is not None:
                self.pool_event_sizes.append(float(event.get("pool_size", 0)))
        for category, stats in record.content_stats.items():
            for def_id, values in stats.items():
                row = self.content[category].get(def_id)
                if row:
                    row["acquisition_count"] += int(values.get("acquisition_count", 0))
                    trigger_count = int(values.get("trigger_count", 0))
                    if trigger_count:
                        row["trigger_count"] += trigger_count
                        row["triggered_games"] += 1
                        if record.won:
                            row["wins_when_triggered"] += 1
                    consumed_count = int(values.get("consumed_count", 0))
                    if consumed_count:
                        row["consumed_count"] += consumed_count
                        row["consumed_games"] += 1
                        if record.won:
                            row["wins_when_consumed"] += 1
        for category, ids in record.selected_content.items():
            for def_id in ids:
                row = self.content[category].get(def_id)
                if row:
                    row["selected_games"] += 1
                    if record.won:
                        row["wins_when_selected"] += 1

        held_by_category = {
            "items": Counter(record.held_items),
            "ingredients": Counter(record.held_ingredients),
            "equipment": Counter(record.held_equipment),
            "essences": Counter(record.held_essences),
        }
        for category, counts in held_by_category.items():
            for def_id, amount in counts.items():
                row = self.content[category].get(def_id)
                if not row:
                    continue
                row["final_owned_count"] += amount
                row["final_owned_games"] += 1
                if record.won:
                    row["wins_when_owned"] += 1

    def _finalize_content(self, win_rate: float) -> None:
        threshold = max(10, self.games // 50)
        for rows in self.content.values():
            for row in rows.values():
                offers = int(row["offer_count"])
                selected_games = int(row["selected_games"])
                owned_games = int(row["final_owned_games"])
                row["selection_rate"] = row["choice_count"] / offers if offers else 0.0
                row["game_selection_rate"] = selected_games / self.games if self.games else 0.0
                row["possession_rate"] = owned_games / self.games if self.games else 0.0
                if selected_games:
                    row["win_rate_when_selected"] = row["wins_when_selected"] / selected_games
                    row["win_lift_when_selected"] = row["win_rate_when_selected"] - win_rate
                if owned_games:
                    row["win_rate_when_owned"] = row["wins_when_owned"] / owned_games
                    row["win_lift_when_owned"] = row["win_rate_when_owned"] - win_rate
                triggered_games = int(row["triggered_games"])
                if triggered_games:
                    row["win_rate_when_triggered"] = row["wins_when_triggered"] / triggered_games
                    row["win_lift_when_triggered"] = row["win_rate_when_triggered"] - win_rate
                else:
                    row["win_rate_when_triggered"] = None
                    row["win_lift_when_triggered"] = None
                consumed_games = int(row["consumed_games"])
                if consumed_games:
                    row["win_rate_when_consumed"] = row["wins_when_consumed"] / consumed_games
                    row["win_lift_when_consumed"] = row["win_rate_when_consumed"] - win_rate
                else:
                    row["win_rate_when_consumed"] = None
                    row["win_lift_when_consumed"] = None

                selected_lift = row["win_lift_when_selected"]
                owned_lift = row["win_lift_when_owned"]
                if offers < threshold:
                    continue
                if (
                    row["selection_rate"] >= 0.65 and selected_lift is not None and selected_lift >= 0.08
                ) or (
                    row["possession_rate"] >= 0.45 and owned_lift is not None and owned_lift >= 0.10
                ):
                    row["suspected_balance"] = "possibly_overpowered"
                elif (
                    row["selection_rate"] <= 0.12 and selected_lift is not None and selected_lift <= -0.05
                ) or (
                    row["possession_rate"] <= 0.02 and owned_lift is not None and owned_lift <= -0.08
                ):
                    row["suspected_balance"] = "possibly_underpowered"

    def build(self, *, base_seed: int, difficulty: int, strategy_name: str, fun_mode: str = "none") -> "SimulationReport":
        wins = sum(record.won for record in self.records)
        losses = sum(record.status == "lost" for record in self.records)
        aborted = sum(record.status == "aborted" for record in self.records)
        win_rate = wins / self.games if self.games else 0.0
        self._finalize_content(win_rate)

        growth_curve: list[dict[str, Any]] = []
        for spin in sorted(self.growth):
            point = self.growth[spin]
            samples = int(point["samples"])
            if not samples:
                continue
            growth_curve.append(
                {
                    "spin": spin,
                    "samples": samples,
                    "average_gold": point["gold_sum"] / samples,
                    "average_pool_size": point["pool_size_sum"] / samples,
                    "average_rarity_multiplier": point["rarity_multiplier_sum"] / samples,
                    "average_items": point["items_sum"] / samples,
                    "average_equipment": point["equipment_sum"] / samples,
                    "average_essences": point["essences_sum"] / samples,
                    "average_roll_tokens": point["roll_tokens_sum"] / samples,
                    "average_remove_tokens": point["remove_tokens_sum"] / samples,
                    "average_essence_tokens": point["essence_tokens_sum"] / samples,
                }
            )

        layer_counts = Counter(
            record.end_layer for record in self.records if record.status == "lost"
        )
        death_layers = [
            {
                "layer": layer,
                "death_count": layer_counts.get(layer, 0),
                "death_rate": layer_counts.get(layer, 0) / self.games if self.games else 0.0,
            }
            for layer in range(1, 14)
        ]
        reason_counts = Counter(
            record.death_reason
            for record in self.records
            if record.death_reason is not None
        )
        max_order = 13 if difficulty >= 10 else 12
        order_progression = []
        for order in range(1, max_order + 1):
            reached = self.order_reached[order]
            died = self.order_died[order]
            order_progression.append({
                "order": order,
                "reached": reached,
                "died": died,
                "conditional_death_rate": died / reached if reached else 0.0,
                "average_gold_gap_at_death": (
                    self.order_death_gap_sum[order] / died if died else None
                ),
            })
        anomalies = []
        for category, rows in self.content.items():
            for row in rows.values():
                if row["suspected_balance"]:
                    anomalies.append(
                        {
                            "category": category,
                            "id": row["id"],
                            "name": row["name"],
                            "flag": row["suspected_balance"],
                            "selection_rate": row["selection_rate"],
                            "possession_rate": row["possession_rate"],
                            "win_lift_when_selected": row["win_lift_when_selected"],
                            "win_lift_when_owned": row["win_lift_when_owned"],
                        }
                    )
        anomalies.sort(
            key=lambda row: max(
                abs(row["win_lift_when_selected"] or 0.0),
                abs(row["win_lift_when_owned"] or 0.0),
            ),
            reverse=True,
        )

        summary = {
            "games_requested": self.games,
            "fun_mode": fun_mode,
            "games_recorded": len(self.records),
            "wins": wins,
            "losses": losses,
            "aborted": aborted,
            "win_rate": win_rate,
            "loss_rate": losses / self.games if self.games else 0.0,
            "aborted_rate": aborted / self.games if self.games else 0.0,
            "average_final_gold": sum(record.gold for record in self.records) / self.games if self.games else 0.0,
            "average_spins": sum(record.spins for record in self.records) / self.games if self.games else 0.0,
            "average_orders_completed": sum(record.orders_completed for record in self.records) / self.games if self.games else 0.0,
            "endless_orders_completed": sum(record.endless_orders_completed for record in self.records),
            "highest_endless_order": max((record.highest_endless_order for record in self.records), default=0),
            "highest_endless_single_turn_gold": max((record.highest_endless_single_turn_gold for record in self.records), default=0),
            "highest_single_turn_gold": max((record.highest_single_turn_gold for record in self.records), default=0),
            "average_max_pool_size": sum(self.max_pool_sizes) / self.games if self.games else 0.0,
            "pool_size_distribution": {
                "0-20": sum(size <= 20 for size in self.max_pool_sizes),
                "21-25": sum(21 <= size <= 25 for size in self.max_pool_sizes),
                "26-30": sum(26 <= size <= 30 for size in self.max_pool_sizes),
                ">30": sum(size > 30 for size in self.max_pool_sizes),
            },
            "pool_over_30_rate": (
                sum(size > 30 for size in self.max_pool_sizes) / self.games
                if self.games else 0.0
            ),
            "average_rolls": self.total_rolls / self.games if self.games else 0.0,
            "average_deletes": self.total_deletes / self.games if self.games else 0.0,
            "pool_origin_counts": dict(self.pool_origin_counts),
            "pool_event_counts": dict(self.pool_event_counts),
            "pool_growth_source_counts": dict(self.pool_growth_source_counts),
            "active_choice_total": int(self.pool_growth_source_counts.get("active_choice", 0)),
            "automatic_generation_total": int(self.pool_growth_source_counts.get("automatic_generation", 0)),
            "pool_band_choice_stats": {
                band: {
                    "choices": int(stats.get("choices", 0)),
                    "selected": int(stats.get("selected", 0)),
                    "skipped": int(stats.get("skipped", 0)),
                    "selection_rate": (
                        stats.get("selected", 0) / stats.get("choices", 1)
                        if stats.get("choices", 0) else 0.0
                    ),
                }
                for band, stats in self.pool_band_choice_counts.items()
            },
            "generator_choice_stats": {
                "offered": self.generator_offer_count,
                "selected": self.generator_selected_count,
                "selection_rate": (
                    self.generator_selected_count / self.generator_offer_count
                    if self.generator_offer_count else 0.0
                ),
            },
            "average_pool_event_size": sum(self.pool_event_sizes) / len(self.pool_event_sizes) if self.pool_event_sizes else 0.0,
            "roll_effective_rate": (
                sum(bool(event.get("effective")) for event in self.roll_events) / len(self.roll_events)
                if self.roll_events else None
            ),
            "death_reasons": dict(reason_counts),
            "order_progression": order_progression,
        }
        return SimulationReport(
            base_seed=base_seed,
            difficulty=difficulty,
            games=self.games,
            strategy=strategy_name,
            summary=summary,
            death_layers=death_layers,
            content={category: list(rows.values()) for category, rows in self.content.items()},
            growth_curve=growth_curve,
            anomalies=anomalies,
            games_detail=[record.to_dict() for record in self.records] if self.retain_details else [],
            notes=[
                "相关性指标不是因果证明；选择策略会影响选择率和持有时通关率。",
                f"疑似强弱标记要求至少观察到 {max(10, self.games // 50)} 次候选出现。",
            ],
            fun_mode=fun_mode,
        )


@dataclass
class SimulationReport:
    base_seed: int
    difficulty: int
    games: int
    strategy: str
    summary: dict[str, Any]
    death_layers: list[dict[str, Any]]
    content: dict[str, list[dict[str, Any]]]
    growth_curve: list[dict[str, Any]]
    anomalies: list[dict[str, Any]]
    games_detail: list[dict[str, Any]]
    notes: list[str]
    fun_mode: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": "crucible-echoes-simulation/v1",
            "config": {
                "base_seed": self.base_seed,
                "difficulty": self.difficulty,
                "games": self.games,
                "strategy": self.strategy,
                "fun_mode": self.fun_mode,
            },
            "summary": self.summary,
            "death_layers": self.death_layers,
            "content": self.content,
            "growth_curve": self.growth_curve,
            "anomalies": self.anomalies,
            "games": self.games_detail,
            "notes": self.notes,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @staticmethod
    def _pct(value: float | None) -> str:
        return "—" if value is None else f"{value * 100:.1f}%"

    @staticmethod
    def _number(value: float | int | None) -> str:
        return "—" if value is None else f"{value:.2f}" if isinstance(value, float) else str(value)

    @staticmethod
    def _safe_cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    def _content_table(self, category: str, title: str) -> list[str]:
        rows = sorted(
            self.content.get(category, []),
            key=lambda row: (-int(row["choice_count"]), -int(row["offer_count"]), row["id"]),
        )
        lines = [
            f"### {title}",
            "",
            "| ID | rarity | offers | choices | acquired | triggers | consumed | final owned | selection | possession | owned win | triggered win | flag |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
        for row in rows:
            lines.append(
                "| {id} | {rarity} | {offers} | {choices} | {acquisitions} | {triggers} | {consumed} | {owned_count} | {selection} | {possession} | {win} | {triggered_win} | {flag} |".format(
                    id=self._safe_cell(row["id"]),
                    rarity=self._safe_cell(row.get("rarity") or "—"),
                    offers=row["offer_count"],
                    choices=row["choice_count"],
                    acquisitions=row["acquisition_count"],
                    triggers=row["trigger_count"],
                    consumed=row["consumed_count"],
                    owned_count=row["final_owned_count"],
                    selection=self._pct(row["selection_rate"]),
                    possession=self._pct(row["possession_rate"]),
                    win=self._pct(row["win_rate_when_owned"]),
                    triggered_win=self._pct(row["win_rate_when_triggered"]),
                    flag=self._safe_cell(row["suspected_balance"] or ""),
                )
            )
        return lines

    def to_markdown(self) -> str:
        summary = self.summary
        lines = [
            "# Crucible Echoes 平衡模拟报告",
            "",
            f"- 模拟局数：{self.games}",
            f"- 基础 seed：`{self.base_seed}`（第 i 局使用可复现派生 seed）",
            f"- 难度：{self.difficulty}",
            f"- 策略：`{self.strategy}`",
            f"- 娱乐模式：`{self.fun_mode}`",
            "",
            "## 总览",
            "",
            "| 指标 | 数值 |",
            "|---|---:|",
            f"| 记录局数 | {summary['games_recorded']} / {summary['games_requested']} |",
            f"| 通关数 | {summary['wins']} |",
            f"| 通关率 | {self._pct(summary['win_rate'])} |",
            f"| 失败数 | {summary['losses']} |",
            f"| 中止数 | {summary['aborted']} |",
            f"| 平均最终金币 | {summary['average_final_gold']:.2f}g |",
            f"| 平均旋转数 | {summary['average_spins']:.2f} |",
            f"| 平均完成订单 | {summary['average_orders_completed']:.2f} |",
            f"| 完成无限订单总数 | {summary['endless_orders_completed']} |",
            f"| 最高无限订单 | {summary['highest_endless_order']} |",
            f"| 无限模式最高单回合金币 | {summary['highest_endless_single_turn_gold']}g |",
            f"| 全局最高单回合金币 | {summary['highest_single_turn_gold']}g |",
            f"| 平均最大池大小 | {summary['average_max_pool_size']:.2f} |",
            f"| 平均 Roll 次数 | {summary['average_rolls']:.2f} |",
            f"| 平均删除次数 | {summary['average_deletes']:.2f} |",
            f"| 有效 Roll 比例 | {self._pct(summary['roll_effective_rate'])} |",
            "",
            "池大小分布（按每局最大池大小）",
            "",
            "| 区间 | 局数 |",
            "|---|---:|",
            *[f"| {bucket} | {count} |" for bucket, count in summary["pool_size_distribution"].items()],
            "",
            "## 各层死亡率",
            "",
            "| 层数 | 死亡局数 | 死亡率（占全部模拟） |",
            "|---:|---:|---:|",
        ]
        for row in self.death_layers:
            lines.append(f"| {row['layer']} | {row['death_count']} | {self._pct(row['death_rate'])} |")

        lines.extend(["", "## 订单级到达与死亡", ""])
        lines.extend([
            "| 订单 | reached | died | 条件死亡率 | 死亡时平均金币差 |",
            "|---:|---:|---:|---:|---:|",
        ])
        for row in summary.get("order_progression", []):
            gap = "—" if row["average_gold_gap_at_death"] is None else f"{row['average_gold_gap_at_death']:.2f}g"
            lines.append(
                f"| {row['order']} | {row['reached']} | {row['died']} | "
                f"{self._pct(row['conditional_death_rate'])} | {gap} |"
            )

        source_labels = {
            "active_choice": "主动抓取",
            "automatic_generation": "成分自动生成",
            "copy": "复制",
            "item_generation": "物品生成",
            "periodic_slag": "周期废渣",
            "other": "其他来源",
        }
        lines.extend(["", "## 池增长来源", ""])
        lines.extend(["| 来源 | 新增张数 |", "|---|---:|"])
        source_counts = summary.get("pool_growth_source_counts", {})
        for source in ("active_choice", "automatic_generation", "copy", "item_generation", "periodic_slag", "other"):
            lines.append(f"| {source_labels[source]} (`{source}`) | {int(source_counts.get(source, 0))} |")

        lines.extend(["", "## 鎸夋睜澶у皬鐨勯€夋嫨鐜?", ""])
        lines.extend([
            "| band | choices | selected | skipped | selection rate |",
            "|---|---:|---:|---:|---:|",
        ])
        band_labels = {"under_15": "<15", "15_19": "15-19", "20_plus": ">=20"}
        for band, label in band_labels.items():
            stats = summary.get("pool_band_choice_stats", {}).get(band, {})
            lines.append(
                f"| {label} | {int(stats.get('choices', 0))} | {int(stats.get('selected', 0))} | "
                f"{int(stats.get('skipped', 0))} | {self._pct(stats.get('selection_rate', 0.0))} |"
            )
        generator_stats = summary.get("generator_choice_stats", {})
        lines.append(
            f"- generator ingredients: offers {int(generator_stats.get('offered', 0))} / "
            f"selected {int(generator_stats.get('selected', 0))} / "
            f"selection rate {self._pct(generator_stats.get('selection_rate', 0.0))}"
        )

        lines.extend(["", "## 主要结束原因", ""])
        if summary["death_reasons"]:
            lines.extend(f"- `{reason}`：{count} 局" for reason, count in sorted(summary["death_reasons"].items()))
        else:
            lines.append("- 没有失败或中止记录。")

        lines.extend(["", "## 金币与属性平均成长曲线", ""])
        lines.extend([
            "| 回合 | 样本数 | 平均金币 | 平均池大小 | 平均稀有度倍率 | 平均道具 | 平均装备 |",
            "|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for row in self.growth_curve:
            lines.append(
                f"| {row['spin']} | {row['samples']} | {row['average_gold']:.2f}g | "
                f"{row['average_pool_size']:.2f} | {row['average_rarity_multiplier']:.3f} | "
                f"{row['average_items']:.2f} | {row['average_equipment']:.2f} |"
            )

        lines.extend(["", "## 自动标记的疑似平衡异常", ""])
        if self.anomalies:
            lines.append("以下只是相关性筛查结果，不会自动修改数值；建议优先人工复核前五项：")
            lines.append("")
            lines.extend(
                f"{index}. `{row['category']}/{row['id']}` — {row['flag']}，"
                f"选择率 {self._pct(row['selection_rate'])}，持有率 {self._pct(row['possession_rate'])}，"
                f"持有时通关率变化 {self._pct(row['win_lift_when_owned'])}。"
                for index, row in enumerate(self.anomalies[:5], 1)
            )
        else:
            lines.append("当前阈值下没有足够证据标记异常。")

        lines.extend(["", "## 道具与装备明细", ""])
        lines.extend(self._content_table("items", "道具"))
        lines.extend(["", *self._content_table("ingredients", "成分")])
        lines.extend(["", *self._content_table("equipment", "装备")])
        lines.extend(["", *self._content_table("essences", "精粹")])
        lines.extend(["", "## 说明", ""])
        lines.extend(f"- {note}" for note in self.notes)
        return "\n".join(lines) + "\n"


@dataclass
class DifficultySweepReport:
    base_seed: int
    games_by_difficulty: dict[int, int]
    strategy: str
    reports: dict[int, SimulationReport]
    adjacent_jumps: list[dict[str, Any]]
    notes: list[str]
    fun_mode: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": "crucible-echoes-simulation-sweep/v1",
            "config": {
                "base_seed": self.base_seed,
                "games_by_difficulty": {str(k): v for k, v in self.games_by_difficulty.items()},
                "strategy": self.strategy,
                "fun_mode": self.fun_mode,
            },
            "win_rate_curve": [
                {
                    "difficulty": difficulty,
                    "games": self.games_by_difficulty[difficulty],
                    "win_rate": self.reports[difficulty].summary["win_rate"],
                    "average_orders_completed": self.reports[difficulty].summary["average_orders_completed"],
                    "average_final_gold": self.reports[difficulty].summary["average_final_gold"],
                    "deaths_layers_8_10": sum(
                        row["death_count"]
                        for row in self.reports[difficulty].death_layers
                        if 8 <= row["layer"] <= 10
                    ),
                }
                for difficulty in sorted(self.reports)
            ],
            "order_progression_by_difficulty": {
                str(difficulty): self.reports[difficulty].summary.get("order_progression", [])
                for difficulty in sorted(self.reports)
            },
            "pool_growth_sources_by_difficulty": {
                str(difficulty): self.reports[difficulty].summary.get("pool_growth_source_counts", {})
                for difficulty in sorted(self.reports)
            },
            "pool_band_choice_stats_by_difficulty": {
                str(difficulty): self.reports[difficulty].summary.get("pool_band_choice_stats", {})
                for difficulty in sorted(self.reports)
            },
            "generator_choice_stats_by_difficulty": {
                str(difficulty): self.reports[difficulty].summary.get("generator_choice_stats", {})
                for difficulty in sorted(self.reports)
            },
            "adjacent_jumps": self.adjacent_jumps,
            "reports": {str(k): v.to_dict() for k, v in self.reports.items()},
            "notes": self.notes,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def to_markdown(self) -> str:
        lines = [
            "# Crucible Echoes 难度批量平衡扫描",
            "",
            f"- 固定 base seed：`{self.base_seed}`",
            f"- 策略：`{self.strategy}`",
            f"- 娱乐模式：`{self.fun_mode}`",
            "- 同一 base seed 在不同难度下使用相同的逐局 seed 派生方式，便于横向对照。",
            "",
            "## 难度 1–10 通关率曲线",
            "",
            "| 难度 | 局数 | 通关率 | 平均完成订单 | 平均最终金币 | 第8–10层死亡数 |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
        curve = self.to_dict()["win_rate_curve"]
        for row in curve:
            lines.append(
                f"| {row['difficulty']} | {row['games']} | {row['win_rate'] * 100:.2f}% | "
                f"{row['average_orders_completed']:.2f} | {row['average_final_gold']:.2f}g | {row['deaths_layers_8_10']} |"
            )
        lines.extend([
            "",
            "## 相邻难度跳变",
            "",
            "| 区间 | 前一档通关率 | 后一档通关率 | 变化 | 标记 |",
            "|---|---:|---:|---:|---|",
        ])
        for row in self.adjacent_jumps:
            lines.append(
                f"| {row['from_difficulty']} → {row['to_difficulty']} | "
                f"{row['from_win_rate'] * 100:.2f}% | {row['to_win_rate'] * 100:.2f}% | "
                f"{row['delta'] * 100:+.2f} 个百分点 | {row['flag']} |"
            )
        lines.extend(["", "## 各难度订单级诊断", ""])
        lines.extend([
            "| 难度 | 订单 | reached | died | 条件死亡率 | 死亡时平均金币差 |",
            "|---:|---:|---:|---:|---:|---:|",
        ])
        for difficulty in sorted(self.reports):
            for row in self.reports[difficulty].summary.get("order_progression", []):
                gap = "—" if row["average_gold_gap_at_death"] is None else f"{row['average_gold_gap_at_death']:.2f}g"
                lines.append(
                    f"| {difficulty} | {row['order']} | {row['reached']} | {row['died']} | "
                    f"{row['conditional_death_rate'] * 100:.1f}% | {gap} |"
                )
        lines.extend(["", "## 各难度池增长来源", ""])
        lines.extend(["| 难度 | 主动抓取 | 成分自动生成 | 复制 | 物品生成 | 周期废渣 | 其他来源 |", "|---:|---:|---:|---:|---:|---:|---:|"])
        source_labels = {
            "active_choice": "主动抓取",
            "automatic_generation": "成分自动生成",
            "copy": "复制",
            "item_generation": "物品生成",
            "periodic_slag": "周期废渣",
            "other": "其他来源",
        }
        for difficulty in sorted(self.reports):
            source_counts = self.reports[difficulty].summary.get("pool_growth_source_counts", {})
            lines.append(
                f"| {difficulty} | " + " | ".join(
                    str(int(source_counts.get(source, 0)))
                    for source in source_labels
                ) + " |"
            )
        lines.extend(["", "## 说明", ""])
        lines.extend(f"- {note}" for note in self.notes)
        lines.extend(["", "各难度的完整明细见同目录下的 `balance_d1` 至 `balance_d10` 报告。"])
        return "\n".join(lines) + "\n"


def run_batch(
    games: int = 1000,
    seed: int = 1,
    difficulty: int = 1,
    *,
    strategy: SimulationStrategy | None = None,
    max_actions: int = 5000,
    catalog: Catalog | None = None,
    retain_details: bool = True,
    fun_mode: str = "none",
) -> SimulationReport:
    if games < 1:
        raise ValueError("模拟局数必须至少为1")
    if max_actions < 1:
        raise ValueError("max_actions必须至少为1")
    policy = strategy or HeuristicStrategy()
    active_catalog = catalog or Catalog.load()
    accumulator = BatchAccumulator(active_catalog, games, retain_details=retain_details)
    for index in range(games):
        game_seed = derive_seed(seed, index)
        record = simulate_game(
            game_seed,
            difficulty,
            strategy=policy,
            game_index=index,
            max_actions=max_actions,
            on_choice=accumulator.observe_choice,
            on_spin=accumulator.observe_spin,
            catalog=active_catalog,
            fun_mode=fun_mode,
        )
        accumulator.observe_record(record)
    return accumulator.build(base_seed=seed, difficulty=difficulty, strategy_name=policy.name, fun_mode=fun_mode)


def run_difficulty_sweep(
    *,
    games_by_difficulty: dict[int, int],
    seed: int = 1,
    strategy: SimulationStrategy | None = None,
    max_actions: int = 5000,
    catalog: Catalog | None = None,
    retain_details: bool = True,
    fun_mode: str = "none",
) -> DifficultySweepReport:
    if not games_by_difficulty:
        raise ValueError("至少需要一个难度")
    if any(not 1 <= difficulty <= 15 for difficulty in games_by_difficulty):
        raise ValueError("难度必须在1到15之间")
    if any(games < 1 for games in games_by_difficulty.values()):
        raise ValueError("每个难度的模拟局数必须至少为1")
    reports: dict[int, SimulationReport] = {}
    policy = strategy or HeuristicStrategy()
    for difficulty in sorted(games_by_difficulty):
        reports[difficulty] = run_batch(
            games=games_by_difficulty[difficulty],
            seed=seed,
            difficulty=difficulty,
            strategy=policy,
            max_actions=max_actions,
            retain_details=retain_details,
            catalog=catalog,
            fun_mode=fun_mode,
        )
    adjacent_jumps: list[dict[str, Any]] = []
    difficulties = sorted(reports)
    for previous, current in zip(difficulties, difficulties[1:]):
        from_rate = float(reports[previous].summary["win_rate"])
        to_rate = float(reports[current].summary["win_rate"])
        delta = to_rate - from_rate
        adjacent_jumps.append({
            "from_difficulty": previous,
            "to_difficulty": current,
            "from_win_rate": from_rate,
            "to_win_rate": to_rate,
            "delta": delta,
            "flag": "large_jump" if abs(delta) >= 0.10 else "normal_range",
        })
    return DifficultySweepReport(
        base_seed=seed,
        games_by_difficulty=dict(sorted(games_by_difficulty.items())),
        strategy=policy.name,
        reports=reports,
        adjacent_jumps=adjacent_jumps,
        notes=[
            "相邻难度跳变使用绝对通关率变化达到10个百分点作为预警线，不代表因果结论。",
            "强弱名单应排除触发样本不足的内容，并单独标记幸存者偏差、选择偏差和构筑偏差。",
        ],
        fun_mode=fun_mode,
    )


def write_report(report: SimulationReport, markdown_path: str | Path, json_path: str | Path | None = None) -> None:
    markdown_target = Path(markdown_path)
    markdown_target.parent.mkdir(parents=True, exist_ok=True)
    markdown_target.write_text(report.to_markdown(), encoding="utf-8")
    if json_path:
        json_target = Path(json_path)
        json_target.parent.mkdir(parents=True, exist_ok=True)
        json_target.write_text(report.to_json(), encoding="utf-8")


def write_sweep_report(
    report: DifficultySweepReport,
    markdown_path: str | Path,
    json_path: str | Path | None = None,
    detail_directory: str | Path | None = None,
) -> None:
    markdown_target = Path(markdown_path)
    markdown_target.parent.mkdir(parents=True, exist_ok=True)
    markdown_target.write_text(report.to_markdown(), encoding="utf-8")
    if json_path:
        json_target = Path(json_path)
        json_target.parent.mkdir(parents=True, exist_ok=True)
        json_target.write_text(report.to_json(), encoding="utf-8")
    if detail_directory is not None:
        detail_target = Path(detail_directory)
        detail_target.mkdir(parents=True, exist_ok=True)
        for difficulty, detail in report.reports.items():
            write_report(
                detail,
                detail_target / f"balance_d{difficulty}.md",
                detail_target / f"balance_d{difficulty}.json",
            )
