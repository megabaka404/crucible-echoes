"""Opt-in, data-driven active operations for the content-aware policy.

These are conservative estimates, not a duplicate rules engine or exact
forecast. Decisions never call RNG, simulate a spin or mutate saved state.
"""
from __future__ import annotations

from .content_strategy import ContentAwareV2RevisionStrategy


class ContentActionStrategy(ContentAwareV2RevisionStrategy):
    name = "heuristic-v2-content-v3"

    def _addition_value(self, engine, ids):
        total = 0.0
        size = len(engine.s.ingredients)
        for offset, def_id in enumerate(ids):
            row = engine.catalog.ingredients.get(def_id)
            if row is None:
                return float("-inf")
            prospective = size + offset
            # A batch is priced as a whole; no temporary pool mutation is
            # needed to account for later entries increasing its cost.
            opportunity = 2.0 if prospective < 20 else 6.0 + min(7.0, (prospective - 20) * 0.5)
            support = self._archetype_fit(engine, row)
            opportunity -= min(2.0, support * 0.5)
            total += self._v2_score(engine, "ingredient", def_id) - opportunity
        return total

    def bundle_index(self, engine, choice):
        options = choice.details.get("options", {})
        ranked = []
        for index, option_id in enumerate(choice.offers, 1):
            ids = options.get(option_id, {}).get("add_ingredients", [])
            ranked.append((self._addition_value(engine, ids), -len(ids), option_id, index))
        return max(ranked)[-1] if ranked else 1

    def _income_estimate(self, engine):
        stable = sum(engine.stable_ingredient_value(x) for x in engine.s.ingredients) * self._draw_fraction(engine)
        flat = sum(float(engine.catalog.items[x].get("per_spin_gold", 0)) for x in engine.s.items)
        return max(0.0, stable + flat)

    def _generator_penalty(self, engine, row):
        if engine.ingredient_generation_disabled():
            return 0.0
        return super()._generator_penalty(engine, row)

    def pre_spin_action(self, engine):
        size = len(engine.s.ingredients)
        for item_id in sorted(set(engine.s.items)):
            row = engine.catalog.items[item_id]
            flag = row.get("toggle_flag")
            if flag == "ingredient_generation_disabled" and not engine.s.flags.get("ingredient_generation_permanently_disabled"):
                unhandled = sum(self._generation_profile(engine.catalog.ingredients[x.def_id])
                                * max(0.0, 1 - self._generator_synergy(engine, engine.catalog.ingredients[x.def_id]) / 3)
                                for x in engine.s.ingredients)
                desired = size >= 20 and unhandled >= 2.0
                if bool(engine.s.flags.get(flag, False)) != desired:
                    return {"action": "toggle", "item_id": item_id}
            active = row.get("active", {})
            if active.get("order_book"):
                target, _ = engine.current_order()
                if engine.s.spins_left == 1 and engine.s.gold >= target and not engine.s.flags.get("order_book_sacrifice"):
                    return {"action": "use", "item_id": item_id}
                continue
            if not active.get("consume"):
                continue  # Unknown repeatable effects are not spammed.
            if active.get("item_choices") or (size < 20 and (active.get("ingredient_choices") or active.get("fixed_ingredient_choices") or active.get("tagged_ingredient_choices"))):
                return {"action": "use", "item_id": item_id}
            rules = active.get("add_ingredients", [])
            rules = [rules] if isinstance(rules, dict) else rules
            ids = [str(rule["id"]) for rule in rules for _ in range(int(rule.get("count", 1)))]
            if ids and self._addition_value(engine, ids) > 0:
                return {"action": "use", "item_id": item_id}
        return None

    def removal_index(self, engine):
        if engine.s.spins_left == 1 and engine.s.tokens.get("remove", 0) > 0:
            target, _ = engine.current_order()
            gap = target - engine.s.gold - self._income_estimate(engine)
            if gap > 0:
                ranked = []
                for index, instance in enumerate(engine.s.ingredients, 1):
                    row = engine.catalog.ingredients[instance.def_id]
                    if not row.get("removable", True):
                        continue
                    removed = row.get("on_removed", {})
                    payout = max(0, instance.stored_gold)
                    if removed.get("reason") in {"any", "manual"}:
                        payout += max(0, int(removed.get("gold", 0)))
                    benefit = payout - max(0, engine.stable_ingredient_value(instance)) * self._draw_fraction(engine)
                    if benefit >= gap:
                        retention = sum(self.instance_retention_components(engine, instance).values())
                        ranked.append((retention, -benefit, index))
                if ranked:
                    return min(ranked)[-1]
        return super().removal_index(engine)
