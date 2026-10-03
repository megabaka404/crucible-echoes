"""Optional item-economy estimates; no balance changes or ID bonuses.

Observed event rates are historical evidence, not guaranteed future triggers.
Zero-event/legacy histories have zero evidence. Unmodelled effects retain the
baseline valuation; this is deliberately not a complete second rules engine.
"""
from __future__ import annotations

from .action_strategy import ContentActionStrategy


ITEM_ECONOMY_FIELDS = ("per_spin_token", "event_bonus", "event_bonus_every", "order_savings")


class ItemEconomyStrategy(ContentActionStrategy):
    name = "heuristic-v2-content-v4"

    @staticmethod
    def event_rate(engine, event):
        spins = max(0, int(engine.s.spin))
        if not spins:
            return 0.0
        count = max(0, float(engine.s.stats.get("event_counts", {}).get(event, 0)))
        return count / spins

    def item_economy_components(self, engine, row):
        horizon = max(4, min(12, int(engine.s.spins_left or 8)))
        # Use the baseline periodic-Token unit and flat-income unit. No token
        # ID is assigned an invisible premium, nor is historical gold absolute.
        token_unit = 3.0
        gold_unit = 1.2
        parts = {"item_recurring_tokens": 0.0, "item_event_income": 0.0,
                 "item_event_rewards": 0.0, "item_order_savings": 0.0}
        token = row.get("per_spin_token", {})
        if token:
            parts["item_recurring_tokens"] = float(token.get("amount", 1)) * horizon * token_unit
        for event, amount in row.get("event_bonus", {}).items():
            parts["item_event_income"] += self.event_rate(engine, event) * horizon * float(amount) * gold_unit
        for event, rule in row.get("event_bonus_every", {}).items():
            expected = self.event_rate(engine, event) * horizon / max(1, int(rule.get("every", 1)))
            reward = float(rule.get("amount", 0)) * gold_unit
            reward += sum(float(amount) for amount in rule.get("tokens", {}).values()) * token_unit
            # Choice-extra effects are not cash/Token rewards. Do not pretend
            # they are worthless, but leave their existing baseline unchanged.
            parts["item_event_rewards"] += expected * reward
        savings = row.get("order_savings", {})
        if savings.get("withdraw_before_failure"):
            expected_orders = self.event_rate(engine, "order_completed") * horizon
            # Only future deposits from this item, never existing savings that
            # already belong to the player. Rescue money is not immediate cash.
            parts["item_order_savings"] = expected_orders * float(savings.get("deposit_on_complete", 0)) * 0.6
        return parts

    def score_components(self, engine, kind, def_id):
        parts = super().score_components(engine, kind, def_id)
        if kind == "item":
            parts.update(self.item_economy_components(engine, engine.catalog.items[def_id]))
        return parts
