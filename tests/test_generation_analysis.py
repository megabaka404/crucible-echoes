from copy import deepcopy
import unittest

from crucible_echoes.catalog import Catalog
from crucible_echoes.generation_analysis import generation_classes
from crucible_echoes.model import PendingChoice
from crucible_echoes.simulation import BatchAccumulator, HeuristicStrategy, simulate_game


class GenerationAnalysisTests(unittest.TestCase):
    def test_fields_not_ids_determine_classes_and_are_pure(self):
        row = {"id": "new_unknown_id", "chance_spawn": {"chance": .3},
               "periodic_spawn": {"every": 8}, "on_removed": {"spawn_tag": "ore"}}
        before = deepcopy(row)
        self.assertEqual(("chance", "periodic", "on_remove"), generation_classes(row))
        self.assertEqual(before, row)
        row["id"] = "another_unknown_id"
        self.assertEqual(("chance", "periodic", "on_remove"), generation_classes(row))

    def test_one_shot_and_removal_do_not_become_continuous_generators(self):
        catalog = Catalog.load()
        self.assertEqual(("on_remove",), generation_classes(catalog.ingredients["nested_chest"]))
        self.assertEqual(("on_remove",), generation_classes(catalog.ingredients["conical_flask"]))
        self.assertEqual(("potion_once",), generation_classes(catalog.ingredients["recycle_potion"]))
        self.assertEqual(("potion_once",), generation_classes(catalog.ingredients["copy_potion"]))
        self.assertEqual(("script_unclassified",), generation_classes(catalog.ingredients["pickaxe"]))

    def test_non_generators_explicit_optout_and_dynamic_unknown_are_not_guessed(self):
        self.assertEqual((), generation_classes({"spawn_each_spin": {}, "id": "x"}))
        self.assertEqual((), generation_classes({"ingredient_generation": False, "chance_spawn": {"chance": 1}}))
        self.assertEqual((), generation_classes({"ingredient_generation": 0, "chance_spawn": {"chance": 1}}))
        self.assertEqual((), generation_classes({"ingredient_generation": None, "chance_spawn": {"chance": 1}}))
        self.assertEqual((), generation_classes({"script": "unknown_script"}))
        self.assertEqual(("script_unclassified",), generation_classes({"ingredient_generation": True}))

    def test_raw_slots_and_selection_are_separate_from_narrow_legacy_generator_metric(self):
        class PickPolicy(HeuristicStrategy):
            def choose(self, engine, choice):
                return 1
        def start(engine):
            engine.s.pending = [PendingChoice("ingredient", ["nested_chest", "copy_potion", "pickaxe"])]
        summaries = []
        for retain in (True, False):
            accumulator = BatchAccumulator(Catalog.load(), 1, retain_details=retain)
            record = simulate_game(11, strategy=PickPolicy(), on_start=start,
                                   on_choice=accumulator.observe_choice, max_actions=1)
            accumulator.observe_record(record)
            report = accumulator.build(base_seed=11, difficulty=1, strategy_name="test")
            summaries.append(report.summary)
            counts = report.summary["generator_capability_stats"]
            self.assertEqual((1, 1), (counts["on_remove"]["offered"], counts["on_remove"]["selected"]))
            self.assertEqual((1, 0), (counts["potion_once"]["offered"], counts["potion_once"]["selected"]))
            self.assertEqual((1, 0), (counts["script_unclassified"]["offered"], counts["script_unclassified"]["selected"]))
            self.assertEqual(0, report.summary["generator_choice_stats"]["offered"])
            self.assertIn("生成能力分类型曝光", report.to_markdown())
        self.assertEqual(summaries[0], summaries[1])
