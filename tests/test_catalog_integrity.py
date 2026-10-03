import json
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from crucible_echoes.catalog import Catalog


class CatalogIntegrityTests(unittest.TestCase):
    def test_duplicate_ids_cannot_silently_overwrite_content(self):
        for name in ("ingredients", "items", "essences"):
            payloads = {f"{kind}.json": [{"id": kind, "name": kind}]
                        for kind in ("ingredients", "items", "essences")}
            payloads["progression.json"] = {}
            payloads[f"{name}.json"] *= 2
            root = SimpleNamespace(joinpath=lambda filename: SimpleNamespace(
                read_text=lambda **kwargs: json.dumps(payloads[filename])))
            package = SimpleNamespace(joinpath=lambda _: root)
            with patch("crucible_echoes.catalog.files", return_value=package):
                with self.assertRaisesRegex(ValueError, "Duplicate"):
                    Catalog.load()

    def test_current_catalog_remains_valid(self):
        self.assertEqual([], Catalog.load().validate())

    def test_normal_content_rarity_cannot_be_silently_truncated_or_out_of_range(self):
        for collection, def_id in (("ingredients", "water"), ("items", "lucky_charm")):
            for rarity in (2.77, True, 0, 5, None):
                with self.subTest(collection=collection, rarity=rarity):
                    catalog = deepcopy(Catalog.load())
                    getattr(catalog, collection)[def_id]["rarity"] = rarity
                    errors = catalog.validate()
                    self.assertEqual(1, len(errors))
                    self.assertIn("rarity", errors[0])

    def test_zero_rarity_is_only_legal_for_non_offerable_system_ingredients(self):
        catalog = deepcopy(Catalog.load())
        self.assertEqual([], catalog.validate())
        catalog.ingredients["slag"]["offerable"] = True
        self.assertEqual(1, len(catalog.validate()))

    def test_missing_generated_and_transformed_ingredients_have_exact_paths(self):
        catalog = deepcopy(Catalog.load())
        catalog.ingredients["vein"]["periodic_spawn"]["id"] = "missing_ore"
        catalog.ingredients["grass_seed"]["transform_after"]["into"] = "missing_plant"
        catalog.ingredients["egg"]["chance_transforms"][1]["into"] = "missing_egg"
        errors = catalog.validate()
        self.assertEqual(3, len(errors))
        self.assertTrue(any("vein periodic_spawn.id" in e and "missing_ore" in e for e in errors))
        self.assertTrue(any("grass_seed transform_after.into" in e for e in errors))
        self.assertTrue(any("egg chance_transforms[1].into" in e for e in errors))

    def test_missing_item_and_essence_targets_are_checked(self):
        catalog = deepcopy(Catalog.load())
        catalog.items["test_tube_rack"]["bonuses"][0]["id"] = "missing_tube"
        catalog.items["sandpaper_box"]["active"]["add_ingredients"]["id"] = "missing_paper"
        catalog.essences["easter_egg_box_essence"]["effect"]["add_ingredient"]["id"] = "missing_egg"
        errors = catalog.validate()
        self.assertEqual(3, len(errors))
        self.assertTrue(any("bonuses[0].id" in e for e in errors))
        self.assertTrue(any("active.add_ingredients.id" in e for e in errors))
        self.assertTrue(any("effect.add_ingredient.id" in e for e in errors))

    def test_bundle_option_names_are_not_ingredient_ids_but_contents_are(self):
        catalog = deepcopy(Catalog.load())
        item = next(row for row in catalog.items.values() if row.get("on_acquire", {}).get("bundle_choice"))
        option = item["on_acquire"]["bundle_choice"]["options"][0]
        option["id"] = "arbitrary_ui_option"
        self.assertEqual([], catalog.validate())
        option["add_ingredients"][1] = "missing_pigment"
        errors = catalog.validate()
        self.assertEqual(1, len(errors))
        self.assertIn("on_acquire.bundle_choice.options[0].add_ingredients[1]", errors[0])

    def test_validation_is_pure_and_does_not_treat_tags_as_ids(self):
        catalog = deepcopy(Catalog.load())
        catalog.items["test_tube_rack"]["protect"]["tag"] = "future_content_tag"
        catalog.items["test_tube_rack"]["bonuses"][0]["ids"] = ["water", "missing_target"]
        before = deepcopy(catalog)
        errors = catalog.validate()
        self.assertEqual(1, len(errors))
        self.assertIn("bonuses[0].ids[1]", errors[0])
        self.assertEqual(before, catalog)

    def test_invalid_reference_list_does_not_silently_pass(self):
        catalog = deepcopy(Catalog.load())
        catalog.ingredients["egg"]["chance_transforms"] = {"into": "water"}
        errors = catalog.validate()
        self.assertEqual(1, len(errors))
        self.assertIn("chance_transforms must be a list", errors[0])
