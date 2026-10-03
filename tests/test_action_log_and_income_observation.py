from copy import deepcopy
import json
import unittest

from crucible_echoes.engine import GameEngine, GameError
from crucible_echoes.model import GameState, PendingChoice


class ActionObservationTests(unittest.TestCase):
    def fresh(self):
        engine = GameEngine()
        engine.new_game(20261035)
        engine.s.last_log = ['previous action must not leak']
        return engine

    def reload(self, engine):
        engine._sync_rng()
        return GameEngine(engine.catalog).bind(GameState.from_dict(json.loads(json.dumps(engine.s.to_dict()))))

    def test_manual_removal_preserves_payout_and_listener_logs(self):
        engine = self.fresh()
        engine.s.ingredients.clear()
        engine.add_ingredient('nested_chest', emit=False)
        engine.s.items.append('warehouse_manager')
        engine.s.tokens['remove'] = 1
        before = engine.s.gold
        engine.remove(1)
        self.assertEqual(12, engine.s.gold - before)
        self.assertEqual('删除了箱中箱。', engine.s.last_log[0])
        self.assertIn('移除箱中箱：+10g。', engine.s.last_log)
        self.assertIn('仓库管理员：+2g。', engine.s.last_log)
        self.assertNotIn('previous action must not leak', engine.s.last_log)
        self.assertEqual(engine.s.to_dict(), self.reload(engine).s.to_dict())

    def test_active_exchange_preserves_acquisition_listener_log(self):
        engine = self.fresh()
        engine.s.items.extend(['brown_reagent', 'easter_egg_box'])
        before = engine.s.gold
        engine.use_item('easter_egg_box')
        self.assertEqual(3, engine.s.gold - before)
        self.assertEqual('使用了彩蛋盒。', engine.s.last_log[0])
        self.assertIn('棕色试剂：+3g。', engine.s.last_log)
        self.assertNotIn('previous action must not leak', engine.s.last_log)

    def test_choice_preserves_entire_current_chain_not_last_three(self):
        engine = self.fresh()
        engine.s.items.append('venture_capital')
        engine.s.pending = [PendingChoice('bundle', ['accept'], can_skip=False,
            details={'options': {'accept': {'name': '整组接受', 'add_ingredients': ['water'] * 5}}})]
        engine.s.gold = 20
        engine.choose(1)
        self.assertEqual(15, engine.s.gold)
        self.assertEqual('选择了整组接受。', engine.s.last_log[0])
        self.assertEqual(5, engine.s.last_log.count('风险投资：-1g。'))
        self.assertNotIn('previous action must not leak', engine.s.last_log)

    def test_skip_and_reroll_preserve_event_listener_logs(self):
        for command in ('skip', 'reroll'):
            with self.subTest(command=command):
                engine = self.fresh()
                # Synthetic declarative listener tests the general log boundary,
                # not a new card or changed production balance definition.
                engine.catalog = deepcopy(engine.catalog)
                event = 'skip_ingredient' if command == 'skip' else 'reroll'
                engine.catalog.items['worker']['event_bonus'] = {event: 7}
                engine.s.items.append('worker')
                engine.s.tokens['roll'] = 1
                engine.s.pending = [PendingChoice('ingredient', ['water', 'paper'])]
                before = engine.s.gold
                getattr(engine, command)()
                self.assertEqual(before + 7, engine.s.gold)
                self.assertIn('打工人：+7g。', engine.s.last_log)
                self.assertNotIn('previous action must not leak', engine.s.last_log)

    def test_rejected_actions_preserve_logs_and_rng(self):
        for command, args in (('remove', (99,)), ('use_item', ('worker',)),
                              ('choose', (99,)), ('skip', ()), ('reroll', ())):
            with self.subTest(command=command):
                engine = self.fresh()
                before = deepcopy(engine.s.to_dict()), engine.r.state
                with self.assertRaises(GameError):
                    getattr(engine, command)(*args)
                self.assertEqual(before, (engine.s.to_dict(), engine.r.state))

    def test_income_predicates_do_not_invent_zero_before_any_settlement(self):
        for essence in ('coin_jar_essence', 'portable_scale_essence', 'white_reagent_essence'):
            for legacy in (False, True):
                with self.subTest(essence=essence, legacy=legacy):
                    engine = self.fresh()
                    if legacy:
                        engine.s.spin = 7  # Legacy record without observed income.
                        engine = self.reload(engine)
                    self.assertNotIn('last_income', engine.s.stats)
                    engine.add_essence(essence)
                    before = engine.s.gold, engine.r.state
                    engine.check_essences()
                    self.assertIn(essence, engine.s.essences)
                    self.assertEqual(before, (engine.s.gold, engine.r.state))

    def test_real_zero_income_round_still_triggers_and_survives_reload(self):
        for essence in ('coin_jar_essence', 'portable_scale_essence', 'white_reagent_essence'):
            with self.subTest(essence=essence):
                live = self.fresh()
                live.s.ingredients.clear()
                self.assertEqual(0, live.spin())
                resumed = self.reload(live)
                for engine in (live, resumed):
                    before = engine.s.gold
                    engine.add_essence(essence)
                    engine.check_essences()
                    self.assertEqual(before + 30, engine.s.gold)
                    self.assertIn(essence, engine.s.consumed_essences)
                self.assertEqual(live.s.to_dict(), resumed.s.to_dict())

    def test_income_guard_does_not_block_unrelated_immediate_essence(self):
        engine = self.fresh()
        before = len(engine.s.ingredients)
        engine.add_essence('easter_egg_box_essence')
        engine.check_essences()
        self.assertEqual(before + 3, len(engine.s.ingredients))
        self.assertIn('easter_egg_box_essence', engine.s.consumed_essences)


if __name__ == '__main__':
    unittest.main()
