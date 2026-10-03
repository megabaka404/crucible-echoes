"""Pure action-boundary differences for UI feedback, not an effect trace.

Same-action births/deaths and intermediate transformations are not observed.
Never infer an effect source, removal reason, or chronological ordering here.
"""
from __future__ import annotations

from collections import Counter


def action_changes(before, after):
    old = {x.uid: x for x in before.s.ingredients}
    new = {x.uid: x for x in after.s.ingredients}

    def identity(engine, instance):
        return {"uid": instance.uid, "id": instance.def_id,
                "name": engine.catalog.ingredients[instance.def_id]["name"]}

    changes = []
    for uid in sorted(old.keys() | new.keys()):
        if uid not in old:
            changes.append({"kind": "added", **identity(after, new[uid])})
        elif uid not in new:
            changes.append({"kind": "removed", **identity(before, old[uid])})
        else:
            previous, current = old[uid], new[uid]
            if previous.def_id != current.def_id:
                changes.append({"kind": "transformed", **identity(after, current),
                                "previous_id": previous.def_id,
                                "previous_name": before.catalog.ingredients[previous.def_id]["name"]})
            delta = current.permanent_bonus - previous.permanent_bonus
            if delta:
                changes.append({"kind": "permanent", **identity(after, current),
                                "before": previous.permanent_bonus, "after": current.permanent_bonus,
                                "delta": delta})
    consumed = Counter(after.s.consumed_essences) - Counter(before.s.consumed_essences)
    old_items, new_items = Counter(before.s.items), Counter(after.s.items)
    items = [{"kind": kind, "id": key, "name": after.catalog.items[key]["name"], "count": count}
             for kind, counts in (("added", new_items - old_items), ("removed", old_items - new_items))
             for key, count in sorted(counts.items())]
    return {"scope": "action_boundary", "ingredients": changes,
            "items": items,
            "gold": {"before": before.s.gold, "after": after.s.gold, "delta": after.s.gold - before.s.gold},
            "tokens": {token: {"before": before.s.tokens.get(token, 0), "after": after.s.tokens.get(token, 0),
                               "delta": after.s.tokens.get(token, 0) - before.s.tokens.get(token, 0)}
                       for token in sorted(before.s.tokens.keys() | after.s.tokens.keys())},
            "consumed_essences": [{"id": key, "name": after.catalog.essences[key]["name"], "count": count}
                                  for key, count in sorted(consumed.items())]}
