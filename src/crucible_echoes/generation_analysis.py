"""Read-only capability classes; never used to secretly score specific IDs.

Script-only generators without declarative lifecycle information remain
unclassified. A death-triggered or one-shot generator must not be labelled
as continuous future pool pollution merely because it can create a component.
"""
from typing import Any

GENERATION_CLASSES = ("per_spin", "chance", "periodic", "on_remove", "potion_once", "script_unclassified")


def generation_classes(definition: dict[str, Any]) -> tuple[str, ...]:
    if "ingredient_generation" in definition and not definition["ingredient_generation"]:
        return ()
    classes = []
    for field, kind in (("spawn_each_spin", "per_spin"), ("chance_spawn", "chance"), ("periodic_spawn", "periodic")):
        if definition.get(field):
            classes.append(kind)
    if isinstance(definition.get("on_removed"), dict) and definition["on_removed"].get("spawn_tag"):
        classes.append("on_remove")
    potion = definition.get("potion", {})
    if isinstance(potion, dict) and potion.get("recycle"):
        classes.append("potion_once")
    if not classes and definition.get("ingredient_generation"):
        # Potion-triggered components are removed after their effect. Other
        # script markers alone cannot tell us how many/net new slots result.
        classes.append("potion_once" if "potion" in definition.get("tags", []) else "script_unclassified")
    return tuple(classes)
