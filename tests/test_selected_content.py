"""Regression coverage for the accepted September content additions."""
import unittest
from unittest.mock import patch

from crucible_echoes.engine import GameEngine
from crucible_echoes.model import GameState
from crucible_echoes.rng import DeterministicRNG


class SelectedContentTests(unittest.TestCase):
    def board(self, *ids):
        engine = GameEngine()
        engine.new_game(20260920)
        engine.s.ingredients.clear()
        engine.s.pending.clear()
        engine.s.gold = 0
        instances = [engine.add_ingredient(i, emit=False) for i in ids]
        engine._board = instances
        engine._coords = [(0, i) for i in range(len(ids))]
        return engine, instances

    def test_legacy_growth_uses_normal_period_and_preserves_single_choice_rng(self):
        for source, target, period in (("polishing_wheel", "copper", 8), ("strengthening_elixir", "water", 10)):
            engine, (grower, a, b) = self.board(source, target, target)
            engine._coords = [(0, 1), (0, 0), (0, 2)]
            grower.counter = period - 1
            engine._run_script(0, grower, None)
            self.assertEqual(0, a.permanent_bonus + b.permanent_bonus)
            grower.counter = period
            expected_rng = DeterministicRNG(engine.r.state)
            chosen = expected_rng.choice([a, b])
            engine._run_script(0, grower, None)
            self.assertEqual(1, chosen.permanent_bonus)
            self.assertEqual(expected_rng.state, engine.r.state)
            self.assertEqual(0, grower.counter)
            self.assertEqual(1, engine._round_events["periodic"])

    def test_legacy_growth_supports_timer_and_initial_counter(self):
        engine, (elixir, target) = self.board("strengthening_elixir", "water")
        self.assertEqual(5, elixir.counter)
        engine.s.items.append("timer")
        elixir.counter = 9
        engine._run_script(0, elixir, None)
        self.assertEqual(1, target.permanent_bonus)

    def test_flame_emits_one_burn_event_per_successful_target(self):
        for fuel, reward in (("charcoal", 40), ("alcohol", 50)):
            engine, (flame, target) = self.board("flame", fuel)
            engine._values = [0, 4]
            engine._flame(0)
            self.assertNotIn(target, engine.s.ingredients)
            self.assertEqual(reward, engine.s.gold)
            self.assertEqual(1, engine._round_events["burned"])

    def test_flame_does_not_reward_or_generate_for_protected_target(self):
        for fuel in ("charcoal", "alcohol"):
            engine, (flame, target, guard) = self.board("flame", fuel, "restraint")
            engine._values = [0, 4, 1]
            engine._flame(0)
            self.assertIn(target, engine.s.ingredients)
            self.assertEqual(0, engine.s.gold)
            self.assertEqual(0, engine._round_events["burned"])
            self.assertFalse(any(x.def_id == "ash" for x in engine.s.ingredients))

    def test_golden_key_survives_a_blocked_opening(self):
        engine, (key, chest, guard) = self.board("golden_key", "wood_chest", "restraint")
        engine._golden_key(0, key)
        self.assertIn(key, engine.s.ingredients)
        self.assertIn(chest, engine.s.ingredients)
        self.assertEqual(0, engine.s.gold)
        self.assertEqual(0, engine._round_events["opened"])
        engine._golden_key(0, key)
        self.assertNotIn(key, engine.s.ingredients)
        self.assertNotIn(chest, engine.s.ingredients)
        self.assertEqual(20, engine.s.gold)
        self.assertEqual(1, engine._round_events["opened"])

    def test_flask_only_generates_when_shattered(self):
        for reason, count in (("removed", 0), ("shattered", 1)):
            engine, (flask,) = self.board("conical_flask")
            engine._remove(flask, reason, 0)
            self.assertEqual(count, len(engine.s.ingredients))
            if count:
                self.assertIn("liquid", engine.catalog.ingredients[engine.s.ingredients[0].def_id]["tags"])

    def test_universal_chest_requires_opening(self):
        for reason, reward in (("removed", 0), ("opened", 1)):
            engine, (chest,) = self.board("universal_chest")
            before = dict(engine.s.tokens)
            engine._remove(chest, reason, 0)
            self.assertEqual(reward, len(engine.s.items))
            for token in ("roll", "remove", "essence"):
                self.assertEqual(before[token] + reward, engine.s.tokens[token])

    def test_consumption_skips_a_previously_removed_neighbor(self):
        engine, (cat, gone, live) = self.board("kitten", "milk", "milk")
        engine._coords = [(0, 1), (0, 0), (0, 2)]
        engine._remove(gone, "consumed", 1)
        self.assertTrue(engine._consume_first(0, {"milk"}, 9))
        self.assertNotIn(live, engine.s.ingredients)
        self.assertEqual(9, engine.s.gold)

    def test_growth_magic_probability_is_data_driven_and_needs_neighbor(self):
        for chance in (0.05, 0.15):
            for roll, expected in ((chance - 0.001, 1), (chance, 0)):
                engine, (magic, target) = self.board("growth_magic", "water")
                engine.catalog.ingredients["growth_magic"]["growth_chance"] = chance
                with patch.object(engine.r, "random", return_value=roll):
                    engine._run_script(0, magic, "growth_magic")
                self.assertEqual(expected, target.permanent_bonus)
        engine, (magic,) = self.board("growth_magic")
        before = engine.r.state
        engine._run_script(0, magic, "growth_magic")
        self.assertEqual(before, engine.r.state)

    def test_mercenary_does_not_get_paid_for_protected_monster(self):
        engine, (mercenary, monster, guard) = self.board("mercenary", "goblin", "restraint")
        engine._run_script(0, mercenary, "mercenary")
        self.assertIn(monster, engine.s.ingredients)
        self.assertIn(mercenary, engine.s.ingredients)
        self.assertEqual(0, engine.s.gold)
        self.assertEqual(0, engine._round_events["removed"])

    def test_lamp_paper_burning_emits_burn_event(self):
        for fuel in ("paper", "sandpaper"):
            engine, (lamp, target) = self.board("alcohol_lamp", fuel)
            engine._run_script(0, lamp, "alcohol_lamp")
            self.assertNotIn(target, engine.s.ingredients)
            self.assertEqual(18, engine.s.gold)
            self.assertEqual(1, engine._round_events["burned"])
            self.assertEqual(1, engine._round_events["removed"])
        engine, (lamp, target) = self.board("alcohol_lamp", "oil")
        engine._run_script(0, lamp, "alcohol_lamp")
        self.assertEqual(0, engine._round_events["burned"])

    def test_glass_shard_only_pays_when_expiring(self):
        for reason, expected in (("expired", 6), ("removed", 0)):
            engine, (shard,) = self.board("glass_shard")
            self.assertTrue(engine._remove(shard, reason, 0))
            self.assertEqual(expected, engine.s.gold)
            self.assertFalse(engine._remove(shard, reason, 0))
            self.assertEqual(expected, engine.s.gold)

    def test_envelope_token_only_on_expiry(self):
        for reason, expected in (("expired", 1), ("removed", 0)):
            engine, (envelope,) = self.board("envelope")
            before = engine.s.tokens["roll"]
            engine._remove(envelope, reason, 0)
            self.assertEqual(before + expected, engine.s.tokens["roll"])

    def test_parchment_growth_survives_save(self):
        engine, (paper,) = self.board("parchment")
        paper.age = paper.counter = 5
        restored = GameEngine().bind(GameState.from_dict(engine.s.to_dict()))
        restored.spin()
        self.assertEqual(1, restored.s.ingredients[0].permanent_bonus)

    def test_clockmaker_distinct_targets_and_no_target_safe(self):
        engine, (maker, a, b) = self.board("clockmaker", "spring", "spring")
        engine._coords = [(0, 1), (0, 0), (0, 2)]
        maker.age = maker.counter = 4
        engine._run_script(0, maker, None)
        self.assertEqual((1, 1), (a.permanent_bonus, b.permanent_bonus))
        engine, (maker,) = self.board("clockmaker")
        maker.age = maker.counter = 4
        engine._run_script(0, maker, None)

    def test_periodic_growth_respects_timer_and_forced_trigger(self):
        engine, (paper,) = self.board("parchment")
        engine.s.items.append("timer")
        paper.counter = 5
        engine._run_script(0, paper, None)
        self.assertEqual(1, paper.permanent_bonus)
        self.assertEqual(0, paper.counter)
        self.assertEqual(1, engine._round_events["periodic"])
        engine._run_script(0, paper, None)
        self.assertEqual(1, paper.permanent_bonus)
        engine._run_script(0, paper, None, force_periodic=True)
        self.assertEqual(2, paper.permanent_bonus)

    def test_parchment_waits_six_appearances_without_timer(self):
        engine, (paper,) = self.board("parchment")
        for count in range(1, 6):
            paper.counter = count
            engine._run_script(0, paper, None)
            self.assertEqual(0, paper.permanent_bonus)
        paper.counter = 6
        engine._run_script(0, paper, None)
        self.assertEqual(1, paper.permanent_bonus)
        self.assertEqual(0, paper.counter)

    def test_glass_shard_expires_on_tenth_appearance(self):
        engine, (shard,) = self.board("glass_shard")
        for appearance in range(1, 11):
            engine.s.pending.clear()
            engine.s.spins_left = 100
            before = engine.s.gold
            engine.spin()
            if appearance < 10:
                self.assertIn(shard, engine.s.ingredients)
                self.assertEqual(appearance, shard.age)
                self.assertEqual(1, engine.s.gold - before)
            else:
                self.assertNotIn(shard, engine.s.ingredients)
                self.assertEqual(7, engine.s.gold - before)

    def test_crow_consumes_multiple_coins_but_not_lucky_coin(self):
        engine, (crow, a, b, lucky) = self.board("crow", "coin", "coin", "lucky_coin")
        engine._coords = [(1, 1), (0, 1), (1, 0), (1, 2)]
        engine._run_script(0, crow, "crow")
        self.assertEqual(18, engine.s.gold)
        self.assertIn(lucky, engine.s.ingredients)
        self.assertNotIn(a, engine.s.ingredients)
        self.assertNotIn(b, engine.s.ingredients)

    def test_picture_frame_essence_consumes_and_grows_existing_pigments(self):
        engine, pigments = self.board("red_pigment", "yellow_pigment", "blue_pigment")
        engine.add_essence("picture_frame_essence")
        engine.check_essences()
        self.assertIn("picture_frame_essence", engine.s.consumed_essences)
        self.assertEqual([2, 2, 2], [x.permanent_bonus for x in pigments])
        engine.check_essences()
        self.assertEqual([2, 2, 2], [x.permanent_bonus for x in pigments])


if __name__ == "__main__":
    unittest.main()
