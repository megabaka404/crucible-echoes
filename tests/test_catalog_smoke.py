"""Catalog-wide interaction smoke checks, not win-rate/balance measurements."""
import unittest

from crucible_echoes.catalog import Catalog
from crucible_echoes.engine import GameEngine
from crucible_echoes.model import GameState
from crucible_echoes.simulation import validate_simulation_state


class CatalogSmokeTests(unittest.TestCase):
    def test_every_item_and_essence_across_modes_and_difficulty_boundaries(self):
        catalog = Catalog.load()
        modes = ("none", "giant", "rapid", "blind_box", "minimal", "mutation")
        for mode in modes:
            for difficulty in (1, 15):
                for kind, definitions in (("item", catalog.items), ("essence", catalog.essences)):
                    for index, def_id in enumerate(definitions):
                        with self.subTest(mode=mode, difficulty=difficulty, kind=kind, definition=def_id):
                            engine = GameEngine(catalog)
                            engine.new_game(92000 + index, difficulty=difficulty, fun_mode=mode)
                            engine.s.gold = 10000
                            getattr(engine, "add_" + kind)(def_id)
                            for _ in range(100):
                                if not engine.s.pending:
                                    break
                                engine.choose(1)
                            else:
                                self.fail("acquisition choice loop exceeded 100 actions")
                            engine.spin()
                            self.assertEqual([], validate_simulation_state(engine))
                            saved = engine.s.to_dict()
                            restored = GameEngine(catalog).bind(GameState.from_dict(saved))
                            self.assertEqual(saved, restored.s.to_dict())

    def test_every_ingredient_across_modes_and_difficulty_boundaries(self):
        catalog = Catalog.load()
        modes = ("none", "giant", "rapid", "blind_box", "minimal", "mutation")
        for mode in modes:
            for difficulty in (1, 15):
                for index, ingredient_id in enumerate(catalog.ingredients):
                    with self.subTest(mode=mode, difficulty=difficulty, ingredient=ingredient_id):
                        engine = GameEngine(catalog)
                        engine.new_game(91000 + index, difficulty=difficulty, fun_mode=mode)
                        engine.s.ingredients.clear()
                        engine.s.pending.clear()
                        engine.s.gold = 10000
                        for def_id in (ingredient_id, "water", "copper", "charcoal"):
                            engine.add_ingredient(def_id, emit=False)
                        # Exercise countdown boundaries in a synthetic fixture;
                        # these are not legal starting builds or balance samples.
                        for inst in engine.s.ingredients:
                            inst.age = inst.counter = 9
                        engine.spin()
                        self.assertEqual([], validate_simulation_state(engine))
                        saved = engine.s.to_dict()
                        restored = GameEngine(catalog).bind(GameState.from_dict(saved))
                        self.assertEqual(saved, restored.s.to_dict())
