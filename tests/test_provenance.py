import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from copy import deepcopy
from zipfile import ZipFile

from crucible_echoes.catalog import Catalog
from crucible_echoes.engine import GameEngine
from crucible_echoes.provenance import capture_snapshot


class ProvenanceTests(unittest.TestCase):
    def test_snapshot_is_allowlisted_and_hashes_match(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "run.snapshot.zip"
            catalog = Catalog.load()
            result = capture_snapshot(target, catalog)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), result["sha256"])
            with ZipFile(target) as archive:
                for name, digest in result["files"].items():
                    self.assertTrue(name == "catalog.json" or (name.startswith("crucible_echoes/") and name.endswith(".py"))
                                    or name in {f"crucible_echoes/data/{x}.json" for x in ("ingredients", "items", "essences", "progression")})
                    self.assertEqual(digest, hashlib.sha256(archive.read(name)).hexdigest())
                data = json.loads(archive.read("catalog.json"))
                self.assertEqual(catalog.ingredients, data["ingredients"])
                self.assertNotIn(".env", archive.namelist())

    def test_extracted_snapshot_runs_in_isolated_process_with_passed_catalog_order(self):
        catalog = deepcopy(Catalog.load())
        catalog.ingredients["water"]["base"] = 999  # Never written to real card tables.
        reference = GameEngine(catalog)
        reference.new_game(314159, 7)
        reference.spin()
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "snapshot.zip"
            capture_snapshot(target, catalog)
            extracted = Path(temp) / "package"
            with ZipFile(target) as archive:
                rows = json.loads(archive.read("crucible_echoes/data/ingredients.json"))
                self.assertEqual(list(catalog.ingredients), [x["id"] for x in rows])
                archive.extractall(extracted)  # Only our explicit allowlisted archive.
            code = "import sys,json; sys.path.insert(0,sys.argv[1]); from crucible_echoes.catalog import Catalog; from crucible_echoes.engine import GameEngine; c=Catalog.load(); assert not c.validate(); assert c.ingredients['water']['base']==999; e=GameEngine(c); e.new_game(314159,7); e.spin(); print(json.dumps(e.s.to_dict()))"
            result = subprocess.run([sys.executable, "-I", "-c", code, str(extracted)], cwd=temp,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(reference.s.to_dict(), json.loads(result.stdout))
