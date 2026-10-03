import unittest
from unittest.mock import patch

from crucible_echoes.catalog import Catalog
from crucible_echoes.engine import GameEngine
from crucible_echoes.model import PendingChoice
from crucible_echoes.simulation import BatchAccumulator, HeuristicStrategy, simulate_game, strategy_from_name


class SimulationTelemetryTests(unittest.TestCase):
    def candidate_fixture(self, *, retain=True, skip=False, fail=False):
        catalog = Catalog.load()
        accumulator = BatchAccumulator(catalog, 1, retain_details=retain)
        class TestPolicy(HeuristicStrategy):
            def __init__(self):
                super().__init__()
                self.rolls = 0
            def should_reroll(self, engine, choice):
                self.rolls += 1
                return self.rolls <= 2
            def choose(self, engine, choice):
                if fail:
                    return 999
                return None if skip else 1
        def start(engine):
            engine.s.pending = [PendingChoice("ingredient", ["vein", "vein", "water"])]
            engine.s.tokens["roll"] = 2
        def reroll(engine):
            engine.s.tokens["roll"] -= 1
            engine.s.pending[0].offers = (["water", "water", "water"] if engine.s.tokens["roll"] == 1
                                          else ["vein", "water", "water"])
        with patch.object(GameEngine, "reroll", reroll):
            record = simulate_game(77, catalog=catalog, strategy=TestPolicy(), on_start=start,
                                   on_choice=accumulator.observe_choice, max_actions=3)
        accumulator.observe_record(record)
        return record, accumulator.build(base_seed=77, difficulty=1, strategy_name="test")

    def test_candidate_slots_include_discarded_rerolls_and_duplicates(self):
        record, report = self.candidate_fixture()
        self.assertEqual(["rerolled", "rerolled", "resolved"],
                         [e["outcome"] for e in record.strategy_events["offer_exposures"]])
        self.assertEqual(["vein", "water", "water"], record.strategy_events["choices"][0]["offer_ids"])
        for def_id, offers, selected in (("vein", 3, 1), ("water", 6, 0)):
            row = next(r for r in report.content["ingredients"] if r["id"] == def_id)
            self.assertEqual((offers, selected), (row["offer_count"], row["choice_count"]))
            self.assertEqual(offers, record.content_stats["ingredients"][def_id]["offer_count"])
        stats = report.summary["generator_choice_stats"]
        self.assertEqual((3, 1, 2, 0, 1), tuple(stats[k] for k in
                         ("offered", "resolved_offered", "rerolled_offered", "unresolved_offered", "selected")))
        self.assertEqual(1 / 3, stats["selection_rate"])
        periodic = report.summary["generator_capability_stats"]["periodic"]
        self.assertEqual((3, 2, 1), tuple(periodic[k] for k in ("offered", "rerolled_offered", "selected")))
        self.assertEqual(1, report.summary["pool_band_choice_stats"]["under_15"]["choices"])
        self.assertIn("重调丢弃候选", report.to_markdown())

    def test_skipped_and_failed_choices_still_have_exposures(self):
        for skip, fail, last_outcome in ((True, False, "resolved"), (False, True, "unresolved")):
            with self.subTest(skip=skip, fail=fail):
                record, report = self.candidate_fixture(skip=skip, fail=fail)
                self.assertEqual(last_outcome, record.strategy_events["offer_exposures"][-1]["outcome"])
                row = next(r for r in report.content["ingredients"] if r["id"] == "vein")
                self.assertEqual((3, 0), (row["offer_count"], row["choice_count"]))
                self.assertEqual(int(fail), report.summary["generator_choice_stats"]["unresolved_offered"])

    def test_exposure_coverage_survives_summary_only_and_marks_legacy(self):
        _, full = self.candidate_fixture()
        _, compact = self.candidate_fixture(retain=False)
        self.assertEqual(full.summary, compact.summary)
        self.assertEqual(full.content, compact.content)
        self.assertEqual((1, 0), tuple(full.summary["offer_tracking"][k]
                                     for k in ("tracked_games", "legacy_untracked_games")))
        record = simulate_game(43, max_actions=1)
        record.strategy_events.pop("offer_tracking")
        record.strategy_events.pop("offer_exposures")
        accumulator = BatchAccumulator(Catalog.load(), 1)
        accumulator.observe_record(record)
        summary = accumulator.build(base_seed=43, difficulty=1, strategy_name="legacy").summary
        self.assertEqual((0, 1), tuple(summary["offer_tracking"][k]
                                     for k in ("tracked_games", "legacy_untracked_games")))

    def test_invalid_offer_aborts_but_report_aggregation_does_not_crash(self):
        def start(engine):
            engine.s.pending = [PendingChoice("ingredient", ["not_a_definition"])]
        record = simulate_game(44, on_start=start, max_actions=1)
        self.assertEqual("aborted", record.status)
        accumulator = BatchAccumulator(Catalog.load(), 1)
        accumulator.observe_record(record)
        report = accumulator.build(base_seed=44, difficulty=1, strategy_name="test")
        self.assertEqual(1, report.summary["aborted"])
        self.assertEqual("unresolved", record.strategy_events["offer_exposures"][0]["outcome"])

    def test_normal_removal_is_observed_even_if_no_final_holder(self):
        def start(engine):
            engine.s.ingredients.clear()
            engine.s.pending.clear()
            shard = engine.add_ingredient("glass_shard", emit=False)
            shard.age = 9
        record = simulate_game(42, on_start=start)
        stats = record.content_stats["ingredients"]["glass_shard"]
        self.assertGreaterEqual(stats["removal_count"], 1)
        accumulator = BatchAccumulator(Catalog.load(), 1)
        accumulator.observe_record(record)
        report = accumulator.build(base_seed=42, difficulty=1, strategy_name="test")
        row = next(r for r in report.content["ingredients"] if r["id"] == "glass_shard")
        self.assertEqual(stats["removal_count"], row["removal_count"])
        self.assertEqual(1, row["removed_games"])
        self.assertEqual(int(record.won), row["wins_when_removed"])
        self.assertEqual("not_instrumented", row["trigger_tracking"])
        self.assertIn("未统计", report.to_markdown())

    def test_blank_history_has_safe_removal_defaults(self):
        record = simulate_game(43)
        for category in ("ingredients", "equipment"):
            for row in record.content_stats[category].values():
                self.assertGreaterEqual(row["removal_count"], 0)

    def test_active_and_bundle_counts_survive_discarding_action_details(self):
        record = simulate_game(44)
        record.strategy_events["active_actions"] = [
            {"action": "use", "item_id": "large_material_pack"},
            {"action": "toggle", "item_id": "ban"},
            {"action": "toggle", "item_id": "ban"},
        ]
        record.strategy_events["choices"] = [
            {"kind": "bundle", "selected": "unusual_accept_id", "added_count": 3},
            {"kind": "bundle", "selected": "unusual_decline_id", "added_count": 0},
            {"kind": "bundle", "selected": "legacy_uninstrumented"},
        ]
        summaries = []
        for retain in (True, False):
            accumulator = BatchAccumulator(Catalog.load(), 1, retain_details=retain)
            accumulator.observe_record(record)
            summary = accumulator.build(base_seed=44, difficulty=1, strategy_name="test").summary
            summaries.append(summary)
            self.assertEqual(3, summary["active_action_total"])
            self.assertEqual(1, summary["active_action_games"])
            self.assertEqual({"use": 1, "toggle": 2}, summary["active_action_counts"])
            self.assertEqual(2, summary["active_action_item_counts"]["toggle:ban"])
            self.assertEqual({"choices": 3, "accepted": 1, "declined": 1, "untracked": 1}, summary["bundle_choice_counts"])
        self.assertEqual(summaries[0], summaries[1])

    def test_pool_flow_reconciles_in_every_mode_and_is_reproducible(self):
        for mode in ("none", "giant", "rapid", "blind_box", "minimal", "mutation"):
            with self.subTest(mode=mode):
                a = simulate_game(20261008, 7, strategy=strategy_from_name("heuristic-v2-content-v3"), fun_mode=mode)
                b = simulate_game(20261008, 7, strategy=strategy_from_name("heuristic-v2-content-v3"), fun_mode=mode)
                self.assertEqual(a, b)
                flow = a.strategy_events["pool_flow_summary"]
                self.assertTrue(flow["reconciled"])
                self.assertEqual(flow["initial"] + flow["added"] - flow["removed"], flow["final"])
                self.assertEqual(flow["added"], sum(a.strategy_events["pool_source_counts"].values()))
                self.assertEqual(flow["removed"], sum(flow["removal_origin_counts"].values()))
                self.assertEqual(flow["final"], a.final_attributes["pool_size"])
                for event in a.strategy_events["pool_flows"]:
                    self.assertEqual(event["before"] + event["added"] - event["removed"], event["after"])

    def test_skip_triggered_generation_is_observed_without_changing_origin_flags(self):
        class SkipPolicy(HeuristicStrategy):
            def choose(self, engine, choice):
                return None
        catalog = Catalog.load()
        catalog.essences["old_catalog_essence"]["effect"] = {"add_ingredient": {"id": "water", "count": 2}}
        observed = []
        def start(engine):
            engine.s.ingredients.clear()
            engine.add_ingredient("water", emit=False)
            engine.s.pending = [PendingChoice("ingredient", ["water"])]
            engine.add_essence("old_catalog_essence")
        def choice(engine, _choice, _selected):
            observed.extend(dict(inst.flags) for inst in engine.s.ingredients)
        record = simulate_game(11, catalog=catalog, strategy=SkipPolicy(), on_start=start, on_choice=choice, max_actions=1)
        flow = record.strategy_events["pool_flow_summary"]
        self.assertEqual((1, 2, 0, 3), tuple(flow[k] for k in ("initial", "added", "removed", "final")))
        self.assertEqual(2, record.strategy_events["pool_source_counts"]["other"])
        self.assertNotIn("_sim_origin", observed[1])
        self.assertNotIn("_sim_origin", observed[2])

    def test_removed_entries_and_identity_changes_are_separate(self):
        def start(engine):
            engine.s.ingredients.clear()
            engine.add_ingredient("water", emit=False)
            shard = engine.add_ingredient("glass_shard", emit=False)
            shard.age = 9
            engine.s.spins_left = 99
        def spin(engine):
            water = next(inst for inst in engine.s.ingredients if inst.def_id == "water")
            engine._transform(water, "kitten")
        record = simulate_game(12, on_start=start, on_spin=spin, max_actions=1)
        flow = record.strategy_events["pool_flow_summary"]
        self.assertEqual((2, 0, 1, 1, 1), tuple(flow[k] for k in ("initial", "added", "removed", "identity_changes", "final")))
        event = record.strategy_events["pool_removals"][0]
        self.assertEqual("glass_shard", event["before_action_id"])
        self.assertEqual("initial", event["origin"])
        self.assertEqual("spin", event["action"])

    def test_copy_event_outside_spin_is_not_lost_or_reused(self):
        def start(engine):
            engine.s.pending = [PendingChoice("ingredient", ["water"]), PendingChoice("ingredient", ["water"])]
        def choice(engine, _choice, _selected):
            if len(engine.s.pending) == 1:
                engine.add_ingredient("water")
                engine.emit("copied")
            else:
                # An event with no surviving addition must be consumed too.
                engine.emit("copied")
        record = simulate_game(13, on_start=start, on_choice=choice, max_actions=3)
        self.assertEqual(1, record.strategy_events["pool_source_counts"]["copy"])
        self.assertEqual(2, record.strategy_events["pool_source_counts"]["active_choice"])
        self.assertTrue(record.strategy_events["pool_flow_summary"]["reconciled"])

    def test_pool_flow_survives_summary_only_and_legacy_is_untracked(self):
        record = simulate_game(14)
        summaries = []
        for retain in (True, False):
            accumulator = BatchAccumulator(Catalog.load(), 1, retain_details=retain)
            accumulator.observe_record(record)
            report = accumulator.build(base_seed=14, difficulty=1, strategy_name="test")
            summary = report.summary
            summaries.append(summary)
            self.assertEqual(1, summary["pool_flow_tracked_games"])
            self.assertEqual(0, summary["pool_flow_unreconciled_games"])
            self.assertIn("净池变化核对", report.to_markdown())
        self.assertEqual(summaries[0], summaries[1])
        record.strategy_events.pop("pool_flow_summary")
        accumulator = BatchAccumulator(Catalog.load(), 1)
        accumulator.observe_record(record)
        report = accumulator.build(base_seed=14, difficulty=1, strategy_name="test")
        self.assertEqual(0, report.summary["pool_flow_tracked_games"])
        self.assertIn("不能将旧报告缺失值解释为0", report.to_markdown())
