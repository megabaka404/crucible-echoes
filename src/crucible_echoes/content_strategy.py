"""Opt-in content valuation experiment; the original v2 policy is unchanged.

Scores are estimates, not forecasts or a second implementation of game rules.
No definition IDs receive hidden bonuses; this module never draws game RNG.
"""
from __future__ import annotations

from .simulation import HeuristicV2Strategy


class ContentAwareV2Strategy(HeuristicV2Strategy):
    name = "heuristic-v2-content"

    @staticmethod
    def _profitable_expiry(row):
        effect = row.get("on_removed", {})
        return (
            row.get("removable", True)
            and "negative" not in row.get("tags", [])
            and int(row.get("remove_after", 0)) > 0
            and effect.get("reason") in {"expired", "any"}
            and (effect.get("gold", 0) > 0 or any(v > 0 for v in effect.get("tokens", {}).values()))
        )

    def _reject_ingredient(self, row):
        if self._profitable_expiry(row):
            return False
        return super()._reject_ingredient(row)

    @staticmethod
    def _draw_fraction(engine):
        return min(1.0, engine.board_capacity() / max(1, len(engine.s.ingredients) + 1))

    def _long_term_ingredient_value(self, engine, row):
        value = super()._long_term_ingredient_value(engine, row)
        horizon = max(6, min(12, int(engine.s.spins_left) + 4))
        draws = horizon * self._draw_fraction(engine)
        growth = row.get("periodic_permanent_bonus") or row.get("periodic_adjacent_permanent_growth")
        if growth:
            every = max(2, int(growth.get("every", 1)) - sum(
                int(engine.catalog.items[i].get("counter_reduction", 0)) for i in engine.s.items
            ))
            targets = 1.0
            if row.get("periodic_adjacent_permanent_growth"):
                count = sum(not growth.get("tag") or growth["tag"] in engine.catalog.ingredients[x.def_id].get("tags", [])
                            for x in engine.s.ingredients)
                # Sparse matching pools should not look like guaranteed adjacency.
                density = count / max(1, len(engine.s.ingredients))
                targets = min(float(growth.get("targets", 1)), count * self._draw_fraction(engine))
                targets *= min(1.0, density * 4.0)
            triggers = max(0, int(draws // every))
            remaining = sum(max(0, draws - every * i) for i in range(1, triggers + 1))
            value += float(growth.get("amount", 1)) * targets * remaining / horizon * 1.5
        if self._profitable_expiry(row):
            expiry = int(row["remove_after"])
            effect = row["on_removed"]
            reward = float(effect.get("gold", 0)) + 3.0 * sum(effect.get("tokens", {}).values())
            # Replace the old flat on_removed premium with a delay-aware estimate.
            value += reward / expiry * min(1.0, draws / expiry) * 1.5 - 1.5
        return value

    @staticmethod
    def _bonus_matches(bonus, row):
        if bonus.get("rarity") and int(bonus["rarity"]) != int(row.get("rarity", 0)):
            return False
        tags = set(row.get("tags", []))
        return (bonus.get("id") == row["id"] or row["id"] in bonus.get("ids", [])
                or bonus.get("tag") in tags or bool(tags.intersection(bonus.get("tags", [])))
                or ("base" in bonus and bonus["base"] == row.get("base", 0)))

    def score_components(self, engine, kind, def_id):
        parts = super().score_components(engine, kind, def_id)
        if kind == "ingredient":
            row = engine.catalog.ingredients[def_id]
            if "waste" in row.get("tags", []) and self._profitable_expiry(row):
                parts["risk"] += 50.0  # Undo only the inherited blanket waste penalty.
        elif kind == "item":
            row = engine.catalog.items[def_id]
            horizon = max(4, min(12, int(engine.s.spins_left or 8)))
            old = sum(abs(float(b.get("amount", 0))) for b in row.get("bonuses", [])) * horizon * 0.4
            current = sum(float(b.get("amount", 0)) for b in row.get("bonuses", [])
                          for x in engine.s.ingredients
                          if self._bonus_matches(b, engine.catalog.ingredients[x.def_id]))
            parts["long_term"] += current * self._draw_fraction(engine) * horizon * 0.4 - old
        return parts


class ContentAwareV2RevisionStrategy(ContentAwareV2Strategy):
    """Optional successor with explicit build support and instance retention.

    The older policies remain usable as frozen A/B baselines.  Values below
    are conservative estimates derived from visible definitions and owned
    state, never card-specific bonuses or additional game RNG calls.
    """

    name = "heuristic-v2-content-v2"

    @staticmethod
    def _target_matches(spec, row):
        tags = set(row.get("tags", []))
        return (spec.get("id") == row["id"] or row["id"] in spec.get("ids", [])
                or spec.get("tag") in tags or bool(tags.intersection(spec.get("tags", []))))

    @staticmethod
    def _matching_count(engine, *, tag=None, ids=()):
        return sum((tag and tag in engine.catalog.ingredients[x.def_id].get("tags", []))
                   or x.def_id in ids for x in engine.s.ingredients)

    def _archetype_fit(self, engine, row):
        """Shared labels are not enough to bypass a large-pool gate.

        Require an actual payoff target: adjacency/count rules, auras,
        declarative growth targets, or owned item bonuses.  Unrelated glass
        and equipment scripts no longer turn every flask into a core card.
        """
        size = max(1, len(engine.s.ingredients))
        fit = 0.0
        value = row.get("value", {})
        for tag_key, id_key in (("if_adjacent_tag", "if_adjacent_ids"),
                                ("count_adjacent_tag", "count_ids")):
            count = self._matching_count(engine, tag=value.get(tag_key), ids=value.get(id_key, []))
            if count:
                density = min(1.0, count * 4.0 / size)
                reward = float(value.get("bonus", value.get("per", 1)))
                fit += density * min(2.0, max(0.0, reward) * 0.75)
        if value.get("count_id"):
            count = self._matching_count(engine, ids=[value["count_id"]])
            fit += min(3.0, count * max(0.0, float(value.get("per", 1))) * 0.5)
        aura = row.get("aura", {})
        if aura:
            count = sum(self._target_matches(aura, engine.catalog.ingredients[x.def_id])
                        for x in engine.s.ingredients)
            fit += min(4.0, count * max(0.0, float(aura.get("multiplier", 1)) - 1) * 0.75)
        growth = row.get("periodic_adjacent_permanent_growth", {})
        if growth:
            count = self._matching_count(engine, tag=growth.get("tag")) if growth.get("tag") else size
            fit += min(3.0, count * max(0.0, float(growth.get("amount", 1))) * 0.5)
        # Incoming auras and item bonuses are genuine support for this card.
        for instance in engine.s.ingredients:
            incoming = engine.catalog.ingredients[instance.def_id].get("aura", {})
            if incoming and self._target_matches(incoming, row):
                fit += min(2.0, max(0.0, float(incoming.get("multiplier", 1)) - 1))
        for item_id in engine.s.items:
            for bonus in engine.catalog.items[item_id].get("bonuses", []):
                if self._bonus_matches(bonus, row):
                    fit += min(2.0, max(0.0, float(bonus.get("amount", 0))))
        return min(8.0, fit)

    def _growth_chance_value(self, engine, row):
        chance = max(0.0, min(1.0, float(row.get("growth_chance", 0))))
        draws = max(4, min(12, int(engine.s.spins_left or 8))) * self._draw_fraction(engine)
        # Price only a first successful growth, rather than inventing repeated
        # growth for an opaque script whose data does not promise repetition.
        return (1.0 - (1.0 - chance) ** draws) * max(0.0, float(row.get("growth_amount", 1))) * 2.5

    def _long_term_ingredient_value(self, engine, row):
        value = super()._long_term_ingredient_value(engine, row)
        potion = row.get("potion", {})
        if potion:
            gold = float(potion.get("gold", 0))
            value += (gold - abs(gold)) * 0.6
            if potion.get("token"):
                value += max(0.0, float(potion.get("amount", 1))) * 3.0
            if potion.get("purify"):
                value += 3.0 if len(engine.s.ingredients) >= 20 else 0.5
            if potion.get("item_rarity"):
                value += max(1, int(potion["item_rarity"])) * 3.0
        value += self._growth_chance_value(engine, row)
        return value

    def score_components(self, engine, kind, def_id):
        parts = super().score_components(engine, kind, def_id)
        if kind == "ingredient":
            # The legacy mapping exposes pool_pressure as an alias and sums
            # both keys.  Keep the schema but count occupancy exactly once.
            parts["pool_pressure"] = 0.0
            row = engine.catalog.ingredients[def_id]
            current_bonus = sum(float(bonus.get("amount", 0)) for item_id in engine.s.items
                                for bonus in engine.catalog.items[item_id].get("bonuses", [])
                                if self._bonus_matches(bonus, row))
            current_bonus += float(engine.s.flags.get("global_permanent_bonuses", {}).get(def_id, 0))
            parts["immediate"] += current_bonus * 1.5
        return parts

    def _is_exception_candidate(self, engine, def_id):
        row = engine.catalog.ingredients[def_id]
        parts = self.score_components(engine, "ingredient", def_id)
        future = parts.get("immediate", 0) + parts.get("long_term", 0)
        return (self._archetype_fit(engine, row) >= 2.5
                or future >= 8.0
                or (int(row.get("rarity", 1)) >= 3 and float(row.get("base", 0)) >= 3)
                or (self._release_factor(row) < 0.9 and future > 0))

    def instance_retention_components(self, engine, instance):
        """Explain a deletion estimate, including progress and cash-out.

        Stored monster gold is paid on any successful removal, so it is an
        immediate deletion benefit rather than a reason to retain the card.
        A matured permanent bonus, in contrast, raises future board income.
        """
        row = engine.catalog.ingredients[instance.def_id]
        fit = self._archetype_fit(engine, row)
        origin = str(instance.flags.get("_sim_origin", "unknown"))
        parts = {
            "definition": self._v2_score(engine, "ingredient", instance.def_id) + fit,
            "permanent_value": float(instance.permanent_bonus) * 1.5,
            "pool_cost": -self._candidate_pool_cost(engine, instance.def_id, origin) * 2.5,
            "cash_out": -min(8.0, max(0.0, float(instance.stored_gold)) * 0.15),
            "progress": 0.0,
            "spent_growth": 0.0,
            "core_support": 0.0,
        }
        if instance.flags.get("grown"):
            parts["spent_growth"] -= self._growth_chance_value(engine, row)
        for field in ("periodic_permanent_bonus", "periodic_adjacent_permanent_growth", "periodic_gold"):
            spec = row.get(field, {})
            if not spec:
                continue
            every = max(2, int(spec.get("every", 1)) - sum(
                int(engine.catalog.items[i].get("counter_reduction", 0)) for i in engine.s.items))
            reward = max(0.0, float(spec.get("amount", spec.get("gold", 1))))
            if field == "periodic_adjacent_permanent_growth" and spec.get("tag"):
                reward *= min(1.0, self._matching_count(engine, tag=spec["tag"]) * 4.0
                              / max(1, len(engine.s.ingredients)))
            parts["progress"] += min(1.0, max(0, instance.counter) / every) * reward * 1.5
        if self._profitable_expiry(row):
            expiry = int(row["remove_after"])
            remaining = max(1, expiry - instance.age)
            effect = row["on_removed"]
            reward = float(effect.get("gold", 0)) + 3.0 * sum(effect.get("tokens", {}).values())
            parts["progress"] += reward * (1.0 / remaining - 1.0 / expiry)
        if fit >= 2.0 and self._long_term_ingredient_value(engine, row) >= 4.0:
            parts["core_support"] += 4.0
        return parts

    def removal_index(self, engine):
        size = len(engine.s.ingredients)
        if engine.s.tokens.get("remove", 0) <= 0 or size < 25:
            return None
        candidates = []
        for index, instance in enumerate(engine.s.ingredients, 1):
            row = engine.catalog.ingredients[instance.def_id]
            if not row.get("removable", True) or self._is_exception_candidate(engine, instance.def_id):
                continue
            parts = self.instance_retention_components(engine, instance)
            cost = self._candidate_pool_cost(engine, instance.def_id, str(instance.flags.get("_sim_origin", "unknown")))
            candidates.append((sum(parts.values()), cost, index))
        if not candidates:
            return None
        retention, cost, index = min(candidates, key=lambda x: (x[0], x[1], x[2]))
        # Occupancy is already subtracted inside retention.  A separate
        # "cost is high" bypass would still delete a +100g grown card from
        # an otherwise excellent pool, defeating instance-aware valuation.
        if size >= 30 and retention < 9.5:
            return index
        if size >= 25 and retention < 8.0:
            return index
        return None
