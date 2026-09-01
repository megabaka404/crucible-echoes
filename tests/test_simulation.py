from __future__ import annotations

import json
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from crucible_echoes.cli import main
from crucible_echoes.engine import GameEngine
from crucible_echoes.model import PendingChoice
from crucible_echoes.simulation import (
    HeuristicStrategy,
    HeuristicV2Strategy,
    HeuristicV3Strategy,
    HeuristicV31Strategy,
    run_batch,
    run_difficulty_sweep,
    simulate_game,
    strategy_from_name,
)


class SimulationTests(unittest.TestCase):
    def test_same_seed_reproduces_batch_and_strategy(self) -> None:
        first = run_batch(games=6, seed=12345, difficulty=2)
        second = run_batch(games=6, seed=12345, difficulty=2)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(HeuristicStrategy().name, first.strategy)

    def test_batch_finishes_within_action_budget(self) -> None:
        report = run_batch(games=12, seed=77, difficulty=1, max_actions=1000)
        self.assertEqual(12, len(report.games_detail))
        self.assertTrue(all(row["action_count"] <= 1000 for row in report.games_detail))
        self.assertTrue(all(row["status"] in {"won", "lost", "aborted"} for row in report.games_detail))
        self.assertEqual(0, report.summary["aborted"])

    def test_heuristic_pool_policy_uses_generic_soft_cap_and_rolls_weak_choices(self) -> None:
        engine = GameEngine()
        engine.new_game(7, difficulty=1)
        policy = HeuristicStrategy()
        engine.s.tokens["roll"] = 1
        weak = PendingChoice(kind="ingredient", offers=["oil", "oil", "oil"])
        self.assertTrue(policy.should_reroll(engine, weak))
        engine.s.tokens["remove"] = 1
        for _ in range(21):
            engine.add_ingredient("water", emit=False)
        self.assertEqual(26, len(engine.s.ingredients))
        self.assertIsNotNone(policy.removal_index(engine))
        engine.s.ingredients = engine.s.ingredients[:25]
        self.assertIsNone(policy.choose(engine, PendingChoice(kind="ingredient", offers=["oil", "oil", "oil"])))

    def test_heuristic_v2_skips_after_twenty_and_deletes_after_twenty_six(self) -> None:
        engine = GameEngine()
        engine.new_game(7, difficulty=1)
        engine.s.ingredients.clear()
        for _ in range(21):
            engine.add_ingredient("water", emit=False)
        policy = HeuristicV2Strategy()
        self.assertIsNone(policy.choose(engine, PendingChoice(kind="ingredient", offers=["oil"])))

        for _ in range(6):
            engine.add_ingredient("water", emit=False)
        engine.s.tokens["remove"] = 1
        self.assertEqual(1, policy.removal_index(engine))
        self.assertEqual("heuristic-v2", strategy_from_name("heuristic-v2").name)

    def test_heuristic_v2_is_cautious_with_unconnected_generators(self) -> None:
        engine = GameEngine()
        engine.new_game(7, difficulty=1)
        engine.s.ingredients.clear()
        for _ in range(15):
            engine.add_ingredient("water", emit=False)
        policy = HeuristicV2Strategy()
        self.assertIsNone(
            policy.choose(engine, PendingChoice(kind="ingredient", offers=["vein"]))
        )

    def test_heuristic_v2_batch_is_seed_reproducible(self) -> None:
        first = run_batch(games=6, seed=2468, difficulty=1, strategy=HeuristicV2Strategy())
        second = run_batch(games=6, seed=2468, difficulty=1, strategy=HeuristicV2Strategy())
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual("heuristic-v2", first.strategy)

    def test_heuristic_v3_batch_is_seed_reproducible(self) -> None:
        first = run_batch(games=6, seed=2468, difficulty=1, strategy=HeuristicV3Strategy())
        second = run_batch(games=6, seed=2468, difficulty=1, strategy=HeuristicV3Strategy())
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual("heuristic-v3", first.strategy)

    def test_heuristic_v31_is_registered_and_matches_v2_below_eighteen(self) -> None:
        engine = GameEngine()
        engine.new_game(2480, difficulty=1)
        engine.s.ingredients.clear()
        for _ in range(17):
            engine.add_ingredient("water", emit=False)
        v2 = HeuristicV2Strategy()
        v31 = HeuristicV31Strategy()
        for def_id in ("water", "vein", "alchemist"):
            choice = PendingChoice(kind="ingredient", offers=[def_id])
            self.assertEqual(v2.choose(engine, choice), v31.choose(engine, choice))
        self.assertEqual("heuristic-v3.1", strategy_from_name("heuristic-v3.1").name)

    def test_heuristic_v31_has_graduated_pool_bands(self) -> None:
        engine = GameEngine()
        engine.new_game(2481, difficulty=1)
        v2 = HeuristicV2Strategy()
        v3 = HeuristicV3Strategy()
        v31 = HeuristicV31Strategy()
        for pool_size in (18, 19):
            engine.s.ingredients.clear()
            for _ in range(pool_size):
                engine.add_ingredient("water", emit=False)
            choice = PendingChoice(kind="ingredient", offers=["water"])
            self.assertEqual(1, v2.choose(engine, choice))
            self.assertEqual(1, v31.choose(engine, choice))
            self.assertGreater(v31.score(engine, "ingredient", "water"), v3.score(engine, "ingredient", "water"))
        for pool_size in (20, 24, 25):
            engine.s.ingredients.clear()
            for _ in range(pool_size):
                engine.add_ingredient("water", emit=False)
            self.assertIsNone(v31.choose(engine, PendingChoice(kind="ingredient", offers=["water"])))

    def test_heuristic_v31_graduates_generator_penalties_without_forbidding_generators(self) -> None:
        engine = GameEngine()
        engine.new_game(2482, difficulty=1)
        policy = HeuristicV31Strategy()
        continuous = policy._generator_class_and_weight(
            engine, engine.catalog.ingredients["summoner"]
        )
        periodic = policy._generator_class_and_weight(
            engine, engine.catalog.ingredients["alchemist"]
        )
        recursive_periodic = policy._generator_class_and_weight(
            engine, engine.catalog.ingredients["vein"]
        )
        one_time = policy._generator_class_and_weight(
            engine, {"one_time_spawn": {"id": "water", "amount": 1}}
        )
        self.assertGreater(continuous[1], periodic[1])
        self.assertGreater(recursive_periodic[1], periodic[1])
        self.assertGreater(periodic[1], one_time[1])

        engine.s.ingredients.clear()
        for _ in range(25):
            engine.add_ingredient("water", emit=False)
        # A low-frequency generator remains selectable when its expected
        # value is good enough; there is no unconditional "forbidden" gate.
        self.assertEqual(
            1,
            policy.choose(engine, PendingChoice(kind="ingredient", offers=["alchemist"])),
        )

    def test_heuristic_v31_preserves_a_generator_core_exception(self) -> None:
        engine = GameEngine()
        engine.new_game(2483, difficulty=1)
        engine.s.ingredients.clear()
        engine.add_ingredient("growth_magic", emit=False)
        engine.add_ingredient("growth_magic", emit=False)
        for _ in range(18):
            engine.add_ingredient("water", emit=False)
        policy = HeuristicV31Strategy()
        self.assertEqual(
            1,
            policy.choose(engine, PendingChoice(kind="ingredient", offers=["magic_magic"])),
        )

    def test_heuristic_v3_is_registered_and_matches_v2_below_fifteen(self) -> None:
        engine = GameEngine()
        engine.new_game(2468, difficulty=1)
        engine.s.ingredients.clear()
        for _ in range(10):
            engine.add_ingredient("water", emit=False)
        v2 = HeuristicV2Strategy()
        v3 = HeuristicV3Strategy()
        for def_id in ("water", "vein", "alchemist"):
            choice = PendingChoice(kind="ingredient", offers=[def_id])
            self.assertEqual(v2.choose(engine, choice), v3.choose(engine, choice))
        self.assertEqual("heuristic-v3", strategy_from_name("heuristic-v3").name)

    def test_heuristic_v3_increases_skips_at_fifteen_and_twenty(self) -> None:
        engine = GameEngine()
        engine.new_game(2469, difficulty=1)
        engine.s.ingredients.clear()
        v2 = HeuristicV2Strategy()
        v3 = HeuristicV3Strategy()
        for pool_size in (15, 20):
            engine.s.ingredients.clear()
            for _ in range(pool_size):
                engine.add_ingredient("water", emit=False)
            choice = PendingChoice(kind="ingredient", offers=["water"])
            self.assertEqual(1, v2.choose(engine, choice))
            self.assertIsNone(v3.choose(engine, choice))

    def test_heuristic_v3_rejects_unhandled_generators_but_keeps_sinks_and_core(self) -> None:
        engine = GameEngine()
        engine.new_game(2470, difficulty=1)
        engine.s.ingredients.clear()
        for _ in range(20):
            engine.add_ingredient("water", emit=False)
        policy = HeuristicV3Strategy()
        self.assertIsNone(
            policy.choose(engine, PendingChoice(kind="ingredient", offers=["summoner"]))
        )
        self.assertEqual(
            1,
            policy.choose(engine, PendingChoice(kind="ingredient", offers=["alchemist"])),
        )

        engine.s.ingredients.clear()
        engine.add_ingredient("growth_magic", emit=False)
        engine.add_ingredient("growth_magic", emit=False)
        for _ in range(18):
            engine.add_ingredient("water", emit=False)
        self.assertEqual(
            1,
            policy.choose(engine, PendingChoice(kind="ingredient", offers=["magic_magic"])),
        )

    def test_heuristic_v3_prefers_cleanup_and_exposes_pool_band_telemetry(self) -> None:
        engine = GameEngine()
        engine.new_game(2471, difficulty=1)
        engine.s.ingredients.clear()
        for _ in range(20):
            engine.add_ingredient("water", emit=False)
        engine.s.tokens["remove"] = 1
        policy = HeuristicV3Strategy()
        self.assertIsNotNone(policy.removal_index(engine))

        report = run_batch(games=4, seed=2471, difficulty=1, strategy=policy)
        self.assertIn("pool_band_choice_stats", report.summary)
        self.assertIn("15_19", report.summary["pool_band_choice_stats"])
        self.assertIn("generator_choice_stats", report.summary)
        self.assertTrue(all(value >= 0 for value in report.summary["pool_growth_source_counts"].values()))

    def test_pool_cost_uses_provenance_and_releases(self) -> None:
        engine = GameEngine()
        engine.new_game(7, difficulty=1)
        for _ in range(20):
            engine.add_ingredient("water", emit=False)
        policy = HeuristicStrategy()
        active = policy._candidate_pool_cost(engine, "water", "active_choice")
        generated = policy._candidate_pool_cost(engine, "water", "automatic_generation")
        temporary = policy._candidate_pool_cost(engine, "water", "one_time_temporary")
        self.assertGreater(active, generated)
        self.assertGreater(generated, temporary)
        self.assertLess(
            policy._candidate_pool_cost(engine, "reroll_potion", "active_choice"),
            active,
        )

    def test_build_state_and_pool_events_are_data_driven(self) -> None:
        policy = HeuristicStrategy()
        engine = GameEngine()
        engine.new_game(12, difficulty=1)
        engine.add_ingredient("vein", emit=False)
        state = policy.build_state(engine)
        self.assertIn("ore", state["tag_counts"])
        self.assertIn("ore", state["generator_tags"])

        def start_with_generator(game: GameEngine) -> None:
            game.s.ingredients.clear()
            game.add_ingredient("vein", emit=False)
            game.s.gold = 25

        record = simulate_game(12, difficulty=1, max_actions=1000, on_start=start_with_generator)
        events = record.strategy_events["pool_events"]
        self.assertTrue(any(event["source"] == "active_choice" for event in events))
        self.assertTrue(any(source in {"automatic_generation", "summon_or_periodic"} for source in record.strategy_events["pool_origin_counts"]))
        self.assertEqual(
            sum(record.strategy_events["pool_origin_counts"].values()),
            len(record.held_ingredients) + len(record.held_equipment),
        )

    def test_batch_summary_exposes_pool_provenance_telemetry(self) -> None:
        report = run_batch(games=4, seed=12, difficulty=1)
        self.assertIn("pool_origin_counts", report.summary)
        self.assertIn("pool_event_counts", report.summary)
        self.assertIn("pool_growth_source_counts", report.summary)
        self.assertIn("pool_over_30_rate", report.summary)
        self.assertIn("active_choice_total", report.summary)
        self.assertIn("automatic_generation_total", report.summary)
        self.assertEqual(
            {
                "active_choice",
                "automatic_generation",
                "copy",
                "item_generation",
                "periodic_slag",
                "other",
            },
            set(report.summary["pool_growth_source_counts"]),
        )
        self.assertGreaterEqual(sum(report.summary["pool_origin_counts"].values()), 1)
        self.assertGreaterEqual(sum(report.summary["pool_event_counts"].values()), 1)

    def test_order_progression_reports_reached_deaths_and_gold_gap(self) -> None:
        report = run_batch(games=12, seed=77, difficulty=6)
        rows = report.summary["order_progression"]
        self.assertEqual(list(range(1, 13)), [row["order"] for row in rows])
        self.assertEqual(report.summary["losses"], sum(row["died"] for row in rows))
        for row in rows:
            self.assertLessEqual(row["died"], row["reached"])
            self.assertGreaterEqual(row["conditional_death_rate"], 0.0)
            self.assertLessEqual(row["conditional_death_rate"], 1.0)
            if row["average_gold_gap_at_death"] is not None:
                self.assertGreaterEqual(row["average_gold_gap_at_death"], 0.0)

    def test_pool_growth_source_counts_classify_copies(self) -> None:
        def start_with_copy_potion(game: GameEngine) -> None:
            game.s.ingredients.clear()
            game.add_ingredient("copy_potion", emit=False)
            game.add_ingredient("water", emit=False)
            game.add_ingredient("water", emit=False)
            game.s.gold = 10_000

        record = simulate_game(3, difficulty=1, on_start=start_with_copy_potion, max_actions=100)
        source_counts = record.strategy_events["pool_source_counts"]
        self.assertGreaterEqual(source_counts.get("copy", 0), 1)
        self.assertIn("growth_source", record.strategy_events["pool_events"][0])

    def test_report_includes_pool_distribution_summing_to_games(self) -> None:
        report = run_batch(games=10, seed=123, difficulty=1)
        self.assertIn("average_max_pool_size", report.summary)
        self.assertEqual(10, sum(report.summary["pool_size_distribution"].values()))
        self.assertTrue(all("max_pool_size" in row["final_attributes"] for row in report.games_detail))

    def test_simulated_state_has_no_negative_or_out_of_range_values(self) -> None:
        report = run_batch(games=12, seed=88, difficulty=10)
        for row in report.games_detail:
            self.assertGreaterEqual(row["gold"], 0)
            self.assertGreaterEqual(row["end_layer"], 1)
            self.assertLessEqual(row["end_layer"], 13)
            self.assertTrue(all(value >= 0 for value in row["final_attributes"]["tokens"].values()))
            self.assertNotIn("state_invariant:", row.get("error") or "")

    def test_gold_floor_prevents_negative_state_from_negative_effects(self) -> None:
        engine = GameEngine()
        engine.new_game(123, difficulty=1)
        engine.s.gold = 0
        engine._gain_gold(-5, "测试扣款")
        self.assertEqual(0, engine.s.gold)

    def test_report_counts_match_requested_games_and_content_stats(self) -> None:
        games = 15
        report = run_batch(games=games, seed=99, difficulty=1)
        self.assertEqual(games, report.summary["games_requested"])
        self.assertEqual(games, report.summary["games_recorded"])
        self.assertEqual(
            games,
            report.summary["wins"] + report.summary["losses"] + report.summary["aborted"],
        )
        for category in ("items", "ingredients", "equipment", "essences"):
            for row in report.content[category]:
                self.assertLessEqual(row["final_owned_games"], games)
                self.assertGreaterEqual(row["offer_count"], row["choice_count"])
                self.assertGreaterEqual(row["acquisition_count"], row["choice_count"])
        self.assertTrue(all(point["samples"] <= games for point in report.growth_curve))
        self.assertIn("ingredients", report.content)
        self.assertTrue(any(row["id"] == "water" for row in report.content["ingredients"]))

    def test_large_scan_can_drop_per_game_details_but_keep_summary(self) -> None:
        report = run_batch(games=4, seed=101, difficulty=1, retain_details=False)
        self.assertEqual(4, report.summary["games_recorded"])
        self.assertEqual([], report.games_detail)
        self.assertGreaterEqual(report.summary["average_rolls"], 0.0)
        self.assertGreaterEqual(report.summary["average_deletes"], 0.0)

    def test_simulate_cli_writes_human_and_json_reports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            markdown = Path(directory) / "balance.md"
            payload = Path(directory) / "balance.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "simulate",
                        "--games",
                        "2",
                        "--seed",
                        "7",
                        "--report",
                        str(markdown),
                        "--json-report",
                        str(payload),
                    ]
                )
            self.assertEqual(0, code)
            self.assertTrue(markdown.exists())
            self.assertTrue(payload.exists())
            data = json.loads(payload.read_text(encoding="utf-8"))
            self.assertEqual(2, data["summary"]["games_recorded"])
            self.assertIn("ingredients", data["content"])
            self.assertIn("### 成分", markdown.read_text(encoding="utf-8"))
            self.assertIn("自动标记的疑似平衡异常", markdown.read_text(encoding="utf-8"))

    def test_simulate_cli_accepts_heuristic_v2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with contextlib.redirect_stdout(io.StringIO()):
                code = main([
                    "simulate",
                    "--games", "1",
                    "--seed", "7",
                    "--strategy", "heuristic-v2",
                    "--summary-only",
                    "--report", str(root / "v2.md"),
                    "--json-report", str(root / "v2.json"),
                ])
            self.assertEqual(0, code)
            data = json.loads((root / "v2.json").read_text(encoding="utf-8"))
            self.assertEqual("heuristic-v2", data["config"]["strategy"])
            self.assertEqual([], data["games"])

    def test_simulate_cli_accepts_heuristic_v3(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with contextlib.redirect_stdout(io.StringIO()):
                code = main([
                    "simulate",
                    "--games", "1",
                    "--seed", "7",
                    "--strategy", "heuristic-v3",
                    "--summary-only",
                    "--report", str(root / "v3.md"),
                    "--json-report", str(root / "v3.json"),
                ])
            self.assertEqual(0, code)
            data = json.loads((root / "v3.json").read_text(encoding="utf-8"))
            self.assertEqual("heuristic-v3", data["config"]["strategy"])
            self.assertIn("pool_band_choice_stats", data["summary"])
            self.assertEqual([], data["games"])

    def test_simulate_cli_accepts_heuristic_v31(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with contextlib.redirect_stdout(io.StringIO()):
                code = main([
                    "simulate",
                    "--games", "1",
                    "--seed", "7",
                    "--strategy", "heuristic-v3.1",
                    "--summary-only",
                    "--report", str(root / "v31.md"),
                    "--json-report", str(root / "v31.json"),
                ])
            self.assertEqual(0, code)
            data = json.loads((root / "v31.json").read_text(encoding="utf-8"))
            self.assertEqual("heuristic-v3.1", data["config"]["strategy"])
            self.assertEqual([], data["games"])

    def test_single_game_can_use_replacement_strategy(self) -> None:
        class FirstOfferStrategy(HeuristicStrategy):
            name = "first-offer-test"

            def choose(self, engine, choice):
                return 1 if choice.offers else None

        record = simulate_game(42, strategy=FirstOfferStrategy(), max_actions=1000)
        self.assertEqual("first-offer-test", FirstOfferStrategy.name)
        self.assertLessEqual(record.action_count, 1000)

    def test_simulate_game_on_start_hook_runs_before_first_spin(self) -> None:
        seen: list[tuple[int, str]] = []

        def on_start(engine: GameEngine) -> None:
            engine.s.items.append("ore_sorting_table")
            seen.append((engine.s.spin, engine.s.items[-1]))

        record = simulate_game(42, on_start=on_start, max_actions=1000)
        self.assertEqual([(0, "ore_sorting_table")], seen)
        self.assertLessEqual(record.action_count, 1000)

    def test_pool_growth_source_counts_classify_item_generated_choices(self) -> None:
        class FirstOfferStrategy(HeuristicStrategy):
            name = "first-offer-item-source-test"

            def choose(self, engine, choice):
                return 1 if choice.offers else None

        def on_start(engine: GameEngine) -> None:
            engine.add_item("large_reactor")

        record = simulate_game(
            4242,
            strategy=FirstOfferStrategy(),
            on_start=on_start,
            max_actions=1000,
        )
        item_events = [
            event
            for event in record.strategy_events["pool_events"]
            if event.get("growth_source") == "item_generation"
        ]
        self.assertGreaterEqual(len(item_events), 4)
        self.assertGreaterEqual(
            record.strategy_events["pool_source_counts"]["item_generation"],
            4,
        )

    def test_content_report_includes_trigger_and_consumption_telemetry(self) -> None:
        report = run_batch(games=8, seed=123, difficulty=1)
        row = next(item for item in report.content["items"] if item["id"] == "brown_reagent")
        for key in (
            "trigger_count",
            "triggered_games",
            "win_rate_when_triggered",
            "consumed_count",
            "consumed_games",
            "win_rate_when_consumed",
        ):
            self.assertIn(key, row)

    def test_content_report_includes_normal_ingredient_telemetry(self) -> None:
        report = run_batch(games=8, seed=456, difficulty=1)
        self.assertIn("ingredients", report.content)
        row = next(item for item in report.content["ingredients"] if item["id"] == "water")
        self.assertGreaterEqual(row["acquisition_count"], 1)
        self.assertGreaterEqual(row["final_owned_games"], 0)
        self.assertIn("win_rate_when_owned", row)
        self.assertTrue(all("held_ingredients" in game for game in report.games_detail))

    def test_difficulty_sweep_reports_curve_and_adjacent_jumps(self) -> None:
        sweep = run_difficulty_sweep(games_by_difficulty={1: 2, 2: 2, 3: 2}, seed=321)
        data = sweep.to_dict()
        self.assertEqual([1, 2, 3], [row["difficulty"] for row in data["win_rate_curve"]])
        self.assertEqual(2, data["win_rate_curve"][0]["games"])
        self.assertEqual(2, len(data["adjacent_jumps"]))
        self.assertIn("reports", data)

    def test_simulate_sweep_cli_writes_summary_and_detail_reports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            markdown = root / "sweep.md"
            payload = root / "sweep.json"
            details = root / "details"
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "simulate-sweep",
                        "--games-low",
                        "1",
                        "--games-high",
                        "1",
                        "--seed",
                        "7",
                        "--report",
                        str(markdown),
                        "--json-report",
                        str(payload),
                        "--detail-directory",
                        str(details),
                    ]
                )
            self.assertEqual(0, code)
            self.assertTrue(markdown.exists())
            self.assertTrue(payload.exists())
            self.assertEqual(15, len(json.loads(payload.read_text(encoding="utf-8"))["win_rate_curve"]))
            self.assertTrue((details / "balance_d10.json").exists())


if __name__ == "__main__":
    unittest.main()
